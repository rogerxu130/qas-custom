"""Manual parent contact queue and twice-daily reminders to School Admin."""
from collections import defaultdict
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlencode, urlparse

import frappe
from frappe.utils import cint, escape_html, getdate, get_datetime_in_timezone, now_datetime, validate_email_address

from qas_custom.modules.makeup.eligibility import has_regular_or_trial_student
from qas_custom.services.concentrated_makeup import is_makeup_row
from qas_custom.utils.environment import sendmail_or_skip, run_scheduled_or_skip

SETTINGS = "Admin Followup Settings"
CONTACT = "Admin Followup Contact"
DIGEST = "Admin Followup Digest"
COMPLETE = {"Text Message Sent", "Customer Confirmed"}


def require_admin(write=False):
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    if write:
        from qas_custom.services.support_view import reject_support_view_write
        reject_support_view_write()


def brisbane_now():
    return get_datetime_in_timezone("Australia/Brisbane")


def contact_key(row):
    # An old contact never completes a rescheduled/replaced makeup booking.
    values = [row.get(k) for k in ("kind", "reference", "student", "session_id", "session_date", "start_time", "end_time", "campus", "campus_address", "contact_phone")]
    return sha256("|".join(str(v or "") for v in values).encode()).hexdigest()


def build_followups(rows, contacts):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("status") not in {"Cancelled", "Leave"} and row.get("student"):
            grouped[row["session_id"]].append(row)
    items = []
    seen = set()
    for attendance in grouped.values():
        students = {row["student"] for row in attendance}
        solo = len(students) == 1 and not has_regular_or_trial_student(attendance) and any(is_makeup_row(row) for row in attendance)
        for row in attendance:
            trial = row.get("enrollment_type") == "Trial" and row.get("inquiry_type") == "Trial Lesson" and row.get("inquiry_status") in {"Booked", "Rescheduled"} and row.get("inquiry_session") == row["session_id"]
            if not trial and not (solo and is_makeup_row(row)):
                continue
            item = dict(row)
            item["kind"] = "trial" if trial else "solo_makeup"
            item["reference"] = row.get("inquiry") if trial else (row.get("makeup_voucher") or (row.get("source_document") if row.get("source_doctype") == "Makeup Voucher" else row["attendance"]))
            if trial:
                item["contact_phone"] = row.get("inquiry_phone") or ""
                item["contact_name"] = row.get("inquiry_contact") or ""
            key = contact_key(item)
            identity = (item["kind"], item["session_id"], item["student"])
            if identity in seen:
                continue
            seen.add(identity)
            item["key"] = key
            contact = contacts.get(key) or {}
            item["contact_status"] = (row.get("confirmation_status") or "Pending") if trial else (contact.get("contact_status") or "Pending")
            item["recorded_at"] = contact.get("recorded_at")
            item["recorded_by"] = contact.get("recorded_by")
            item["pending"] = item["contact_status"] not in COMPLETE
            item["sms_ready"] = bool(item.get("contact_phone") and item.get("campus_address"))
            items.append(item)
    return sorted(items, key=lambda item: (str(item["session_date"]), str(item["start_time"]), item.get("student_name") or ""))


def build_visit_followups(rows):
    items = []
    for row in rows:
        if row.get("inquiry_status") not in {"Booked", "Rescheduled"}:
            continue
        item = dict(row, kind="visit", session_id=None)
        item["key"] = contact_key(item)
        item["contact_status"] = row.get("confirmation_status") or "Pending"
        item["pending"] = item["contact_status"] not in COMPLETE
        item["sms_ready"] = bool(item.get("contact_phone") and item.get("campus_address") and item.get("start_time"))
        items.append(item)
    return items


def collect_followups(start_date, end_date):
    rows = frappe.db.sql("""
        SELECT a.name AS attendance, a.student, a.enrollment_type, a.status,
            a.source_doctype, a.source_document, a.makeup_voucher,
            s.name AS session_id, s.session_date, w.start_time, w.end_time,
            w.campus, w.course, c.course_name, campus.address AS campus_address,
            student.student_name, p.parent_name AS contact_name, p.mobile_number AS contact_phone,
            i.name AS inquiry, i.inquiry_type, i.status AS inquiry_status,
            i.course_session AS inquiry_session, i.confirmation_status,
            i.contact_phone AS inquiry_phone, i.contact_name AS inquiry_contact
        FROM `tabClass Attendance Entry` a
        JOIN `tabCourse Sessions` s ON s.name=a.course_session
        JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
        LEFT JOIN `tabCourse` c ON c.name=w.course
        LEFT JOIN `tabCampus` campus ON campus.name=w.campus
        LEFT JOIN `tabStudent` student ON student.name=a.student
        LEFT JOIN `tabParent` p ON p.name=student.guardian
        LEFT JOIN `tabInquiry` i ON a.source_doctype='Inquiry' AND i.name=a.source_document
        WHERE s.session_date BETWEEN %s AND %s AND s.status='Scheduled'
            AND a.status NOT IN ('Cancelled', 'Leave')
        ORDER BY s.session_date, w.start_time, a.idx, a.creation
    """, (str(start_date), str(end_date)), as_dict=True)
    visits = build_visit_followups(frappe.db.sql("""
        SELECT i.name AS reference, i.status AS inquiry_status, i.student,
            COALESCE(NULLIF(student.student_name, ''), i.submitted_student_name) AS student_name,
            i.contact_name, i.contact_phone, i.confirmation_status, i.campus,
            campus.address AS campus_address,
            i.current_appointment_date AS session_date,
            i.current_appointment_time AS start_time
        FROM `tabInquiry` i
        LEFT JOIN `tabStudent` student ON student.name=i.student
        LEFT JOIN `tabCampus` campus ON campus.name=i.campus
        WHERE i.inquiry_type='School Visit' AND i.status IN ('Booked', 'Rescheduled')
            AND i.current_appointment_date BETWEEN %s AND %s
    """, (str(start_date), str(end_date)), as_dict=True))
    drafts = build_followups(rows, {})
    if not drafts and not visits:
        return []
    contacts = frappe.get_all(CONTACT, filters={"name": ["in", [row["key"] for row in drafts + visits]]}, fields=["name", "contact_status", "recorded_at", "recorded_by"], limit_page_length=0)
    contact_map = {row["name"]: row for row in contacts}
    for item in visits:
        contact = contact_map.get(item["key"]) or {}
        item["recorded_at"] = contact.get("recorded_at")
        item["recorded_by"] = contact.get("recorded_by")
    return sorted(build_followups(rows, contact_map) + visits,
                  key=lambda item: (str(item["session_date"]), str(item["start_time"]), item.get("student_name") or item.get("contact_name") or ""))


def get_followups(target_date=None, upcoming=0):
    require_admin()
    day = getdate(target_date) if target_date else brisbane_now().date() + timedelta(days=0 if cint(upcoming) else 1)
    end = day + timedelta(days=90) if cint(upcoming) else day
    return {"target_date": str(day), "items": collect_followups(day, end)}


def update_contact(key, target_date, contact_status):
    require_admin(write=True)
    if contact_status not in {"Pending", *COMPLETE}:
        frappe.throw("Invalid contact status.")
    # Recompute eligibility and the appointment snapshot before recording contact.
    day = getdate(target_date)
    current = next((row for row in collect_followups(day, day) if row["key"] == key), None)
    if not current:
        frappe.throw("This booking changed or no longer needs individual follow-up. Refresh the list.")
    if contact_status == "Text Message Sent" and not current["sms_ready"]:
        frappe.throw("A parent phone number and campus address are required before preparing an SMS.")
    if current["kind"] in {"trial", "visit"}:
        from qas_custom.services.inquiry import update_inquiry_confirmation_core
        if current["kind"] == "visit":
            update_inquiry_confirmation_core(current["reference"], contact_status,
                expected_campus=current["campus"], expected_appointment_date=current["session_date"],
                expected_appointment_time=current["start_time"])
        else:
            update_inquiry_confirmation_core(current["reference"], contact_status, expected_course_session=current["session_id"])
    with frappe.cache.lock("qas-followup-contact:" + key, timeout=30, blocking_timeout=10):
        doc = frappe.get_doc(CONTACT, key) if frappe.db.exists(CONTACT, key) else frappe.new_doc(CONTACT)
        doc.name = key
        doc.reference = current["reference"]
        doc.course_session = current["session_id"]
        doc.student = current["student"]
        # Preparing another draft must not undo a family's existing confirmation.
        doc.contact_status = "Customer Confirmed" if contact_status == "Text Message Sent" and current["contact_status"] == "Customer Confirmed" else contact_status
        doc.recorded_by = frappe.session.user
        doc.recorded_at = now_datetime()
        if doc.is_new():
            doc.insert(ignore_permissions=True, set_name=key)
        else:
            doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"key": key, "contact_status": doc.contact_status}


def get_settings():
    require_admin()
    doc = frappe.get_single(SETTINGS)
    return {"enabled": cint(doc.enabled), "recipient": doc.recipient or "", "portal_url": doc.portal_url or "https://portal.queenslandartschool.com"}


def save_settings(enabled, recipient):
    require_admin(write=True)
    recipient = str(recipient or "").strip()
    if recipient:
        validate_email_address(recipient, throw=True)
    if cint(enabled) and not recipient:
        frappe.throw("Enter the reminder email address before enabling reminders.")
    doc = frappe.get_single(SETTINGS)
    doc.enabled = cint(enabled)
    doc.recipient = recipient
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return get_settings()


def digest_key(day, hour, recipient):
    return sha256(f"{day}|{hour}|{recipient.lower()}".encode()).hexdigest()


def digest_message(day, hour, items, portal_url):
    trials = sum(row["kind"] == "trial" for row in items)
    visits = sum(row["kind"] == "visit" for row in items)
    solo = sum(row["kind"] == "solo_makeup" for row in items)
    base = (portal_url or "https://portal.queenslandartschool.com").rstrip("/")
    if urlparse(base).scheme != "https":
        base = "https://portal.queenslandartschool.com"
    url = base + "/school-admin?" + urlencode({"tab": "followups", "date": str(day)})
    heading = "明日待联系" if hour == 10 else "明日仍有未完成的短信跟进"
    return (f"<h2>{heading}</h2><p>{escape_html(str(day))}：还有 {trials} 位试课学生、{visits} 个参观预约和 {solo} 位单人补课学生需要联系。</p>"
            f'<p><a href="{escape_html(url)}">打开手机管理页，逐个发送短信</a></p>'
            "<p>已记录 Message Sent 或 Customer Confirmed 的预约不再计入。发送操作仍由你在手机短信应用内完成。</p>")


def run_digest(now=None):
    now = now or brisbane_now()
    if now.hour not in {10, 18}:
        return {"skipped": True, "reason": "Outside reminder window"}
    settings = frappe.get_single(SETTINGS)
    if not cint(settings.enabled) or not settings.recipient:
        return {"skipped": True, "reason": "Email reminders not configured"}
    day = now.date() + timedelta(days=1)
    key = digest_key(day, now.hour, settings.recipient)
    with frappe.cache.lock("qas-followup-digest:" + key, timeout=120, blocking_timeout=1):
        if frappe.db.exists(DIGEST, key):
            return {"skipped": True, "reason": "Already queued"}
        items = [row for row in collect_followups(day, day) if row["pending"]]
        if not items:
            return {"skipped": True, "reason": "No pending contacts"}
        result = sendmail_or_skip(action="admin_followup_digest", recipients=[settings.recipient],
            subject=f"QAS 明日待联系：{len(items)} 个预约 ({day})",
            message=digest_message(day, now.hour, items, settings.portal_url), delayed=True)
        if result and result.get("skipped"):
            return result
        doc = frappe.get_doc({"doctype": DIGEST, "target_date": day, "send_window": now.hour,
            "recipient": settings.recipient, "item_count": len(items), "status": "Queued"})
        doc.insert(ignore_permissions=True, set_name=key)
        # The email queue and deduplication record commit together. Email Queue handles delivery retries.
        frappe.db.commit()
        return {"queued": True, "count": len(items)}


def scheduled_digest():
    return run_scheduled_or_skip("admin_followup_digest", run_digest)
