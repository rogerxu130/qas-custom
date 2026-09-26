"""Website full-term enrollment. Trial booking side effects are deliberately absent."""
from datetime import date
import json

import frappe
from frappe.utils import cint, getdate, validate_email_address

from qas_custom.services import inquiry as inquiries
from qas_custom.modules.notifications.inquiry_admin_notifications import queue_inquiry_admin_notification

DIRECT = "Direct Enrollment"


class ReviewRequired(Exception):
    """A received application needs an operator; infrastructure errors must still fail."""


def _date(value):
    try:
        parsed = date.fromisoformat(str(value))
        if parsed.isoformat() != str(value):
            raise ValueError("Use extended ISO date format")
        return parsed
    except (ValueError, TypeError):
        raise ReviewRequired("Start date must be a valid YYYY-MM-DD date.")


def _safe_answers(value):
    if isinstance(value, dict):
        return {key: _safe_answers(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in ("secret", "token", "password", "authorization", "api_key"))}
    if isinstance(value, list):
        return [_safe_answers(item) for item in value]
    return value


def _response(doc, duplicate=False):
    return {
        "status": "enrolled" if doc.get("converted_enrollment") else "needs_review" if doc.status == "Needs Review" else doc.status.lower(),
        "duplicate": duplicate,
        "inquiry": doc.name,
        "inquiry_status": doc.status,
        "review_required": doc.status == "Needs Review",
        "enrollment": doc.get("converted_enrollment"),
        "invoice": doc.get("converted_invoice"),
    }


def _student(parent, name, dob):
    name = str(name or "").strip()
    try:
        dob = _date(dob)
    except ReviewRequired:
        raise ReviewRequired("Student date of birth must be a valid YYYY-MM-DD date.")
    if not name or dob > getdate():
        raise ReviewRequired("Check the student's name and date of birth.")
    candidates = frappe.get_all("Student", filters={"guardian": parent, "date_of_birth": dob},
                                fields=["name", "student_name"], limit_page_length=0)
    matches = [row for row in candidates if inquiries._normalize_student_identity_name(row.student_name)
               == inquiries._normalize_student_identity_name(name)]
    if len(matches) > 1:
        raise ReviewRequired("More than one student matches this family, name and date of birth. Select the student manually.")
    if matches:
        return matches[0].name
    return inquiries._resolve_student({"student_name": name, "date_of_birth": dob}, parent, DIRECT)


def _match(payload):
    try:
        return _match_schedule(payload)
    except (ValueError, TypeError, OverflowError):
        raise ReviewRequired("The submitted class time or start date could not be parsed.")


def _match_schedule(payload):
    start = _date(payload.get("start_date"))
    if payload.get("weekly_timeslot"):
        slot_id = str(payload["weekly_timeslot"])
        if not frappe.db.exists("Weekly Timeslot", slot_id):
            raise ReviewRequired("The selected weekly class was not found.")
        slot = frappe.get_doc("Weekly Timeslot", slot_id)
        for key, resolver in (("campus", inquiries._resolve_campus), ("course", inquiries._resolve_course)):
            if payload.get(key) and resolver(payload[key]) != slot.get(key):
                raise ReviewRequired("The selected class does not match the submitted campus or course.")
        if payload.get("class_session"):
            if inquiries._parse_class_language(payload["class_session"]) != (slot.get("class_language") or "English"):
                raise ReviewRequired("The submitted class language does not match the selected class.")
            parsed = inquiries._parse_class_session(payload["class_session"])
            if not parsed or parsed["day_of_week"] != slot.day_of_week or inquiries._normalize_time_string(parsed["start_time"]) != inquiries._normalize_time_string(slot.start_time):
                raise ReviewRequired("The selected class does not match the submitted weekday or time.")
            if parsed.get("end_time") and inquiries._normalize_time_string(parsed["end_time"]) != inquiries._normalize_time_string(slot.end_time):
                raise ReviewRequired("The submitted class end time does not match.")
        if start.strftime("%A") != slot.day_of_week:
            raise ReviewRequired("Start date does not match the selected class weekday.")
        rows = frappe.get_all("Course Sessions", filters={"weekly_timeslot": slot_id, "session_date": start}, pluck="name")
        if len(rows) != 1:
            raise ReviewRequired("Start date must match exactly one actual class session.")
        return rows[0]
    mapped = inquiries._map_trial_form_session({
        "campus": payload.get("campus"), "course": payload.get("course"),
        "submitted_class_session": payload.get("class_session"), "submitted_trial_date": start,
    })
    if not mapped.get("course_session"):
        raise ReviewRequired(str(mapped.get("reason") or "No matching class session.").replace("trial", "enrollment").replace("Trial", "Enrollment"))
    return mapped["course_session"]


def _context(doc, course_session, allow_unresolved=False):
    """Lock student then all remaining sessions, matching the attendance booking lock order."""
    from qas_custom.modules.course_schedule.queries import get_remaining_sessions
    from qas_custom.modules.course_schedule.session_resources import active_rows, classroom_capacity, session_is_future, student_has_conflict
    if (not doc.get("student") and not allow_unresolved) or not doc.get("parent"):
        raise ReviewRequired("Select a parent and student before completing enrollment.")
    if doc.get("student"):
        frappe.db.sql("SELECT name FROM `tabStudent` WHERE name=%s FOR UPDATE", (doc.student,))
    if not course_session or not frappe.db.exists("Course Sessions", course_session):
        raise ReviewRequired("Choose an existing course session.")
    first = frappe.get_doc("Course Sessions", course_session)
    remaining = get_remaining_sessions(first.weekly_timeslot, first.session_date)
    if not remaining:
        raise ReviewRequired("No remaining class sessions were found.")
    for row in sorted(remaining, key=lambda row: row.name):
        frappe.db.sql("SELECT name FROM `tabCourse Sessions` WHERE name=%s FOR UPDATE", (row.name,))
    slot = frappe.get_doc("Weekly Timeslot", first.weekly_timeslot, for_update=True)
    term = frappe.get_doc("Term", slot.term, for_update=True)
    if {row.name for row in remaining} != {row.name for row in get_remaining_sessions(slot.name, first.session_date)}:
        raise ReviewRequired("The class schedule changed. Review the available sessions again.")
    first.reload()
    if slot.status != "Active" or term.status not in {"Upcoming", "Active"}:
        raise ReviewRequired("This class or term is not open for enrollment.")
    if not term.start_date or not term.end_date or not (getdate(term.start_date) <= getdate(first.session_date) <= getdate(term.end_date)):
        raise ReviewRequired("The start session is outside the term dates.")
    if not slot.start_time or not slot.end_time or not session_is_future(first, slot):
        raise ReviewRequired("The start session must be scheduled and must not have started.")
    if getdate(first.session_date).strftime("%A") != slot.day_of_week:
        raise ReviewRequired("The start session date does not match the class weekday.")
    if doc.get("student") and frappe.db.exists("Enrollment", {"student": doc.student, "weekly_timeslot": slot.name, "status": ["in", ["Planned", "Active"]]}):
        raise ReviewRequired("This student already has a Planned or Active enrollment in this class.")
    roster = frappe.get_all("Enrollment", filters={"weekly_timeslot": slot.name, "status": ["in", ["Planned", "Active"]], "enrollment_type": "Full-Term"}, pluck="student")
    capacity = classroom_capacity(slot, lock=True)
    if capacity <= 0:
        raise ReviewRequired("Classroom capacity must be configured before confirming enrollment.")
    if cint(slot.get("ndis_friendly")):
        from qas_custom.services.ndis_friendly import NDIS_FRIENDLY_CAPACITY
        capacity = min(capacity, NDIS_FRIENDLY_CAPACITY)
    # Include planned enrollments without attendance. Count each child once.
    for row in remaining:
        session = frappe.get_doc("Course Sessions", row.name)
        if session.weekly_timeslot != slot.name or not session_is_future(session, slot) or getdate(session.session_date) > getdate(term.end_date):
            raise ReviewRequired("Remaining sessions changed or fall outside the open term; review the schedule.")
        occupants = set(roster) | {item.student for item in active_rows(row.name, lock=True)}
        if len(occupants - {doc.student}) >= capacity:
            raise ReviewRequired("Class is full on {0}; select another class or review its capacity.".format(session.session_date))
        if doc.get("student") and student_has_conflict(doc.student, session, slot, lock=True):
            raise ReviewRequired("This student already has a class at the selected time on {0}.".format(session.session_date))
    return first, slot, remaining


def _complete(doc, context, note=None):
    from qas_custom.modules.enrollment.commands import create_full_term_enrollment, link_invoice_to_enrollment
    from qas_custom.modules.billing.commands import create_prorata_invoice
    from qas_custom.modules.attendance.commands import create_full_term_attendance_entries
    from qas_custom.modules.inquiry.commands import mark_converted
    from qas_custom.modules.inquiry.notes import add_conversion_note, add_conversion_internal_note
    from qas_custom.modules.workflows.trial_conversion import apply_conversion_invoice_note, normalize_conversion_internal_note
    session, slot, remaining = context
    note = normalize_conversion_internal_note(note)
    enrollment = create_full_term_enrollment(doc, session, slot, len(remaining), actor=frappe.session.user)
    invoice = create_prorata_invoice(doc, enrollment, slot.course, slot.term, session.name, len(remaining))
    apply_conversion_invoice_note(invoice, note)
    link_invoice_to_enrollment(enrollment, invoice)
    create_full_term_attendance_entries(remaining, doc.student, enrollment.name)
    doc.course_session = session.name
    doc.campus = slot.campus
    doc.preferred_course = slot.course
    doc.current_appointment_date = session.session_date
    doc.current_appointment_time = slot.start_time
    doc.review_reason = None
    mark_converted(doc, enrollment, invoice)
    add_conversion_internal_note(doc, invoice, note, actor=frappe.session.user)
    add_conversion_note(doc, enrollment, invoice, session, slot, len(remaining), actor=frappe.session.user)
    from qas_custom.modules.notifications.enrollment_terms import queue_enrollment_terms_notice

    queue_enrollment_terms_notice(enrollment, invoice)
    # The request owns the transaction. Do not call the trial conversion workflow,
    # which commits internally and awards trial referral rewards.
    return _response(doc)


def create_webhook(payload=None):
    payload = inquiries._get_payload(payload)
    inquiries._validate_webhook_token(payload)
    key = str(payload.get("external_submission_id") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not key or len(key) > 120:
        frappe.throw("A stable external_submission_id of at most 120 characters is required.")
    validate_email_address(email, throw=True)
    if not email or not str(payload.get("parent_name") or "").strip():
        frappe.throw("Parent name and email are required.")
    existing = frappe.db.get_value("Inquiry", {"external_submission_id": key}, "name", for_update=True)
    if existing:
        doc = frappe.get_doc("Inquiry", existing)
        if doc.inquiry_type != DIRECT or doc.contact_email != email:
            frappe.throw("Submission ID is already used by another application.")
        return _response(doc, duplicate=True)
    doc = frappe.new_doc("Inquiry")
    doc.inquiry_type, doc.status, doc.source = DIRECT, "Needs Review", "Fluent Form"
    doc.flags.defer_admin_notification = True
    doc.external_submission_id = key
    doc.external_form_id = str(payload.get("form_id") or "")
    doc.source_url = payload.get("source_url")
    doc.webhook_source = "Fluent Form"
    allowed = ("external_submission_id", "form_id", "submitted_at", "source_url", "parent_name", "email", "phone", "student_name", "date_of_birth", "campus", "course", "weekly_timeslot", "class_session", "start_date", "form_answers")
    doc.raw_webhook_payload = json.dumps(_safe_answers({key: payload[key] for key in allowed if key in payload}), ensure_ascii=False)
    doc.contact_name, doc.contact_email, doc.contact_phone = payload.get("parent_name"), email, payload.get("phone")
    doc.submitted_student_name = payload.get("student_name")
    doc.submitted_class_session = payload.get("class_session")
    doc.requested_start_date = str(payload.get("start_date") or "")[:140]
    doc.confirmation_status = doc.reminder_status = "Not Required"
    doc.campus = inquiries._resolve_campus(payload.get("campus"))
    doc.preferred_course = inquiries._resolve_course(payload.get("course"))
    # Reserve the unique submission before creating any family or financial record.
    frappe.db.savepoint("direct_submission")
    try:
        doc.insert(ignore_permissions=True)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point="direct_submission")
        existing = frappe.db.get_value("Inquiry", {"external_submission_id": key}, "name", for_update=True)
        if not existing:
            raise
        existing_doc = frappe.get_doc("Inquiry", existing)
        if existing_doc.inquiry_type != DIRECT or existing_doc.contact_email != email:
            frappe.throw("Submission ID is already used by another application.")
        return _response(existing_doc, duplicate=True)
    doc.parent = inquiries._resolve_parent({"email": email, "parent_name": doc.contact_name, "phone": doc.contact_phone}, DIRECT)
    # Serialize new submissions for an existing family before matching students.
    frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE", (doc.parent,))
    try:
        doc.student = _student(doc.parent, payload.get("student_name"), payload.get("date_of_birth"))
        doc.submitted_student_dob = _date(payload.get("date_of_birth"))
        session = _match(payload)
        context = _context(doc, session)
    except ReviewRequired as error:
        doc.review_reason = str(error)
    else:
        first, slot, _remaining = context
        doc.status = "Planned"
        doc.course_session = first.name
        doc.campus = slot.campus
        doc.preferred_course = slot.course
        doc.current_appointment_date = first.session_date
        doc.current_appointment_time = slot.start_time
        doc.review_reason = None
    doc.save(ignore_permissions=True)
    # Register only after classification; the job runs after the request commits.
    queue_inquiry_admin_notification(doc)
    return _response(doc)


def _admin_doc(inquiry):
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    request = getattr(frappe.local, "request", None)
    if request and request.headers.get("X-QAS-Support-View-Token"):
        frappe.throw("Changes are disabled in support view.", frappe.PermissionError)
    doc = frappe.get_doc("Inquiry", inquiry, for_update=True)
    if doc.inquiry_type != DIRECT:
        frappe.throw("This action is only for Direct Enrollment applications.")
    if doc.get("parent"):
        frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE", (doc.parent,))
    if doc.status not in {"Planned", "Needs Review", "Converted"}:
        frappe.throw("This application is no longer awaiting enrollment.")
    return doc


def session_options(inquiry, start_date=None, course=None, campus=None):
    from qas_custom.modules.workflows.trial_conversion import _get_conversion_session_options
    doc = _admin_doc(inquiry)
    return _get_conversion_session_options(doc, start_date, course, campus)


def _manual_student(doc, payload, create=False):
    selected = payload.get("student")
    if selected:
        if frappe.db.get_value("Student", selected, "guardian") != doc.parent:
            frappe.throw("The selected student must belong to this parent.")
        doc.student = selected
    elif create and payload.get("student_name"):
        doc.student = _student(doc.parent, payload.get("student_name"), payload.get("date_of_birth"))
    return doc


def _review_result(doc, reason):
    doc.status = "Needs Review"
    doc.review_reason = str(reason)
    doc.save(ignore_permissions=True)
    # Return normally so the request commits the review state, without creating enrollment.
    return {"review_required": True, "review_reason": doc.review_reason,
            "inquiry": inquiries.build_inquiry_detail(doc.name)}


def preview(inquiry, payload=None):
    from qas_custom.modules.billing.commands import get_prorata_invoice_context
    doc = _admin_doc(inquiry)
    if doc.get("converted_enrollment") or doc.status == "Converted":
        frappe.throw("This application has already been enrolled.")
    payload = inquiries._get_payload(payload)
    _manual_student(doc, payload)
    if payload.get("student_name") and not payload.get("student"):
        doc.student = None
    try:
        context = _context(doc, payload.get("course_session"), allow_unresolved=True)
    except ReviewRequired as error:
        return _review_result(doc, error)
    session, slot, remaining = context
    price = get_prorata_invoice_context(doc, slot.course, len(remaining))
    return {"course_session": session.name, "term": slot.term, "remaining_sessions": len(remaining), "estimated_amount": price["invoice_amount"]}


def complete(inquiry, payload=None):
    doc = _admin_doc(inquiry)
    if doc.get("converted_enrollment"):
        return {"inquiry": inquiries.build_inquiry_detail(doc.name), "duplicate": True}
    payload = inquiries._get_payload(payload)
    try:
        _manual_student(doc, payload, create=True)
        context = _context(doc, payload.get("course_session"))
    except ReviewRequired as error:
        return _review_result(doc, error)
    from qas_custom.modules.billing.commands import get_prorata_invoice_context
    from frappe.utils import flt
    session, slot, remaining = context
    price = get_prorata_invoice_context(doc, slot.course, len(remaining))
    if payload.get("expected_amount") is None or payload.get("expected_sessions") is None or flt(payload["expected_amount"], 2) != flt(price["invoice_amount"], 2) or cint(payload["expected_sessions"]) != len(remaining):
        frappe.throw("The enrollment fee or session count changed. Preview again before confirming.")
    _complete(doc, context, payload.get("internal_note"))
    return {"inquiry": inquiries.build_inquiry_detail(doc.name), "duplicate": False}
