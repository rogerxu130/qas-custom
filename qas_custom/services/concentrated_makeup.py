"""School-selected makeup sessions, backed by the existing voucher/attendance flow."""
from datetime import datetime

import frappe
from qas_custom.modules.course_schedule.queries import get_teacher_name_map
from frappe.utils import add_days, cint, getdate, get_time, now_datetime, today

INACTIVE = {"Cancelled", "Leave"}


def lock_booking(student, voucher_id, session_id):
    # All makeup channels use this order. Locks last until the request commits.
    for doctype, name in (("Student", student), ("Makeup Voucher", voucher_id), ("Course Sessions", session_id)):
        frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (name,))


def session_context(session_id, lock=False):
    session = frappe.get_doc("Course Sessions", session_id, for_update=lock)
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot, for_update=lock)
    return session, slot


def active_rows(session_id, exclude=None, lock=False):
    return frappe.db.sql(
        """SELECT name, student, enrollment_type, source_doctype, makeup_voucher FROM `tabClass Attendance Entry`
        WHERE course_session=%s AND status NOT IN ('Cancelled', 'Leave') AND name != %s"""
        + (" FOR UPDATE" if lock else ""), (session_id, exclude or ""), as_dict=True,
    )


def is_makeup_row(row):
    return row.get("enrollment_type") == "Makeup" or row.get("source_doctype") == "Makeup Voucher" or bool(row.get("makeup_voucher"))


def remaining_places(rows, quota, classroom_capacity):
    """Count children, not historical duplicate rows, as physical occupants."""
    total = len({r["student"] for r in rows})
    makeup = len({r["student"] for r in rows if is_makeup_row(r)})
    return max(0, min(cint(quota) - makeup, cint(classroom_capacity) - total))


def overlaps(start, end, other_start, other_end):
    return get_time(start) < get_time(other_end) and get_time(other_start) < get_time(end)


def student_has_conflict(student, session, slot, exclude=None, lock=False):
    rows = frappe.db.sql(
        """SELECT a.course_session, w.start_time, w.end_time
        FROM `tabClass Attendance Entry` a
        JOIN `tabCourse Sessions` s ON s.name=a.course_session
        JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
        WHERE a.student=%s AND a.status NOT IN ('Cancelled', 'Leave')
          AND s.status != 'Cancelled' AND s.session_date=%s AND a.name != %s"""
        + (" FOR UPDATE" if lock else ""), (student, session.session_date, exclude or ""), as_dict=True,
    )
    return any(r.course_session == session.name or overlaps(slot.start_time, slot.end_time, r.start_time, r.end_time) for r in rows)


def classroom_capacity(slot, lock=False):
    return cint(frappe.db.get_value("Classroom", slot.classroom, "capacity", for_update=lock)) if slot.get("classroom") else 0


def session_is_future(session, slot):
    return session.status == "Scheduled" and datetime.combine(getdate(session.session_date), get_time(slot.start_time)) > now_datetime()


def validate_configuration(session):
    before = session.get_doc_before_save()
    if before and cint(before.get("concentrated_makeup_enabled")) and session.status != "Scheduled":
        session.concentrated_makeup_enabled = 0
    if not cint(session.get("concentrated_makeup_enabled")):
        return
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot)
    course = frappe.get_doc("Course", slot.course)
    if not cint(course.get("is_makeup_course")) or not course.get("accepted_makeup_course"):
        frappe.throw("Choose a dedicated makeup course with accepted voucher courses before opening bookings.")
    if not session_is_future(session, slot):
        frappe.throw("Only future scheduled sessions can open for makeup bookings.")
    quota = cint(session.get("concentrated_makeup_capacity"))
    capacity = classroom_capacity(slot)
    if quota <= 0 or capacity <= 0 or quota > capacity:
        frappe.throw("Set a positive makeup quota no greater than the classroom capacity.")
    rows = active_rows(session.name, lock=True) if not session.is_new() else []
    if len({r.student for r in rows if is_makeup_row(r)}) > quota:
        frappe.throw("The quota cannot be lower than the number of existing makeup bookings.")


def validate_attendance(doc):
    """Shared write boundary: includes normal redemption and admin attendance edits."""
    if doc.get("status") in INACTIVE:
        return
    before = doc.get_doc_before_save()
    if before and before.get("status") not in INACTIVE and all(
        before.get(key) == doc.get(key) for key in ("course_session", "student", "enrollment_type", "makeup_voucher", "source_doctype")
    ):
        return  # Marking existing attendance does not consume another place.
    validate_new_place(doc.student, doc.course_session, "Makeup" if is_makeup_row(doc) else doc.enrollment_type, doc.name)


def validate_new_place(student, session_id, enrollment_type="Makeup", exclude=None):
    frappe.db.sql("SELECT name FROM `tabStudent` WHERE name=%s FOR UPDATE", (student,))
    frappe.db.sql("SELECT name FROM `tabCourse Sessions` WHERE name=%s FOR UPDATE", (session_id,))
    session, slot = session_context(session_id, lock=True)
    if not (cint(session.get("concentrated_makeup_enabled")) or cint(session.get("concentrated_makeup_capacity"))):
        return
    rows = active_rows(session_id, exclude, lock=True)
    if not session_is_future(session, slot):
        frappe.throw("This session is no longer available for booking.")
    if student_has_conflict(student, session, slot, exclude, lock=True):
        frappe.throw("This student already has a class at this time.")
    capacity = classroom_capacity(slot, lock=True)
    if capacity <= 0 or len({r.student for r in rows}) >= capacity:
        frappe.throw("This classroom is full. Please choose another session.")
    if enrollment_type == "Makeup":
        if not cint(session.get("concentrated_makeup_enabled")):
            frappe.throw("Bookings for this makeup session are closed.")
        if remaining_places(rows, session.concentrated_makeup_capacity, capacity) <= 0:
            frappe.throw("This makeup session is full. Please choose another session.")


def get_options(student=None):
    from qas_custom.services.parent_portal_read import _require_parent, _get_parent_students, _validate_student_filter
    from qas_custom.modules.makeup.commands import _build_makeup_voucher_payload, MAKEUP_REDEEMABLE_SESSION_WINDOW_DAYS, _build_redeem_session_payload, _get_voucher_used_by_student
    parent = _require_parent()
    students = _get_parent_students(parent.name)
    selected = _validate_student_filter(student, students) if student else (students[0]["name"] if students else None)
    result = dict(students=students, selected_student=selected, vouchers=[], sessions=[], bookings=[])
    if not selected:
        return result
    names = frappe.get_all("Makeup Voucher", filters={"student": ["in", [r["name"] for r in students]], "status": ["in", ["Valid", "Used"]]}, pluck="name", order_by="expiry_date asc, name asc")
    sessions = {}
    for name in names:
        voucher = frappe.get_doc("Makeup Voucher", name)
        if voucher.status == "Used":
            if not voucher.get("used_on_session") or (_get_voucher_used_by_student(voucher) or voucher.student) != selected:
                continue
            session, slot = session_context(voucher.used_on_session)
            if (cint(session.get("concentrated_makeup_enabled")) or cint(session.get("concentrated_makeup_capacity"))) and session_is_future(session, slot) and any(r.student == selected for r in active_rows(session.name)):
                result["bookings"].append({**_build_redeem_session_payload(session.name), "student": selected, "voucher_id": name})
            continue
        if voucher.get("used_on_session") or (voucher.get("expiry_date") and getdate(voucher.expiry_date) < getdate(today())):
            continue
        result["vouchers"].append({**_build_makeup_voucher_payload(voucher), "original_session": voucher.get("original_session")})
    candidates = frappe.get_all("Course Sessions", filters={
        "concentrated_makeup_enabled": 1, "status": "Scheduled",
        "session_date": ["between", [today(), add_days(today(), MAKEUP_REDEEMABLE_SESSION_WINDOW_DAYS)]],
    }, pluck="name")
    for sid in candidates:
        session, slot = session_context(sid)
        if not session_is_future(session, slot) or student_has_conflict(selected, session, slot):
            continue
        course = frappe.get_doc("Course", slot.course)
        if not cint(course.get("is_makeup_course")):
            continue
        accepted = {r.get("course") for r in course.get("accepted_makeup_course") or []}
        voucher_ids = [v["voucher_id"] for v in result["vouchers"]
                       if v["course"] in accepted and v.get("original_session") != sid]
        if not voucher_ids:
            continue
        spots = remaining_places(active_rows(sid), session.get("concentrated_makeup_capacity"), classroom_capacity(slot))
        sessions[sid] = {**_build_redeem_session_payload(sid), "teacher": get_teacher_name_map([slot.teacher]).get(slot.teacher, slot.teacher) if slot.get("teacher") else None,
                         "spots_left": spots, "voucher_ids": voucher_ids}
    result["sessions"] = sorted(sessions.values(), key=lambda r: (str(r["session_date"]), str(r["start_time"]), r["session_id"]))
    result["bookings"].sort(key=lambda r: (str(r["session_date"]), str(r["start_time"])))
    return result



def accepts_voucher(session_id, voucher_course, lock=False):
    session, slot = session_context(session_id, lock=lock)
    course = frappe.get_doc("Course", slot.course, for_update=lock)
    return bool(cint(course.get("is_makeup_course")) and voucher_course in {
        row.get("course") for row in course.get("accepted_makeup_course") or []
    })


def validate_voucher_target(session_id, voucher_course):
    session, _slot = session_context(session_id, lock=True)
    if cint(session.get("concentrated_makeup_enabled")) or cint(session.get("concentrated_makeup_capacity")):
        if not accepts_voucher(session_id, voucher_course, lock=True):
            frappe.throw("This makeup session does not accept this voucher's course.")


def book(voucher_id, session_id, student):
    from qas_custom.services.parent_portal_read import _require_parent, _get_parent_students
    from qas_custom.services.support_view import reject_support_view_write
    from qas_custom.modules.makeup.commands import redeem_parent_voucher_core
    reject_support_view_write()
    parent = _require_parent()
    return redeem_parent_voucher_core(parent, _get_parent_students(parent.name), voucher_id, session_id, student, concentrated_only=True)


def get_settings(course_session):
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    session, slot = session_context(course_session)
    return {"enabled": cint(session.get("concentrated_makeup_enabled")), "capacity": cint(session.get("concentrated_makeup_capacity")), "is_makeup_course": cint(frappe.db.get_value("Course", slot.course, "is_makeup_course")), "booked_count": len({r.student for r in active_rows(course_session) if is_makeup_row(r)}), "classroom_capacity": classroom_capacity(slot)}


def update_settings(course_session, enabled, capacity):
    from qas_custom.services.school_admin import _require_school_admin
    from qas_custom.services.support_view import reject_support_view_write
    reject_support_view_write()
    _require_school_admin()
    if str(enabled) not in {"0", "1"} or not str(capacity).isdigit():
        frappe.throw("Use an on/off setting and a whole-number capacity.")
    frappe.db.sql("SELECT name FROM `tabCourse Sessions` WHERE name=%s FOR UPDATE", (course_session,))
    session = frappe.get_doc("Course Sessions", course_session, for_update=True)
    # Retain quota on closure so already-booked sessions remain recognisable.
    if not cint(enabled) and cint(session.get("concentrated_makeup_capacity")):
        capacity = session.concentrated_makeup_capacity
    session.concentrated_makeup_enabled = cint(enabled)
    session.concentrated_makeup_capacity = cint(capacity)
    session.save(ignore_permissions=True)
    return get_settings(course_session)
