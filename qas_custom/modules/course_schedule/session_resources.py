"""Shared course-session booking resources."""
from datetime import datetime

import frappe
from frappe.utils import cint, getdate, get_time, now_datetime
from qas_custom.services.class_attendance import create_attendance_entry


def session_context(session_id, lock=False):
    session = frappe.get_doc("Course Sessions", session_id, for_update=lock)
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot, for_update=lock)
    return session, slot


def active_rows(session_id, exclude=None, lock=False):
    return frappe.db.sql(
        """SELECT name, student, enrollment_type, source_doctype, source_document, makeup_voucher FROM `tabClass Attendance Entry`
        WHERE course_session=%s AND status NOT IN ('Cancelled', 'Leave') AND name != %s"""
        + (" FOR UPDATE" if lock else ""), (session_id, exclude or ""), as_dict=True,
    )


def overlaps(start, end, other_start, other_end):
    return get_time(start) < get_time(other_end) and get_time(other_start) < get_time(end)


def student_has_conflict(student, session, slot, exclude=None, lock=False):
    if lock:
        # Lock only this student's attendance rows. A JOIN ... FOR UPDATE would also
        # lock other sessions after the target session and invert the session order.
        attendance = frappe.db.sql(
            """SELECT name, course_session FROM `tabClass Attendance Entry`
            WHERE student=%s AND status NOT IN ('Cancelled', 'Leave') AND name != %s FOR UPDATE""",
            (student, exclude or ""), as_dict=True,
        )
        session_ids = tuple(sorted({row.course_session for row in attendance if row.course_session}))
        if not session_ids:
            return False
        rows = frappe.db.sql(
            """SELECT s.name AS course_session, w.start_time, w.end_time
            FROM `tabCourse Sessions` s
            JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
            WHERE s.name IN %s AND s.status != 'Cancelled' AND s.session_date=%s""",
            (session_ids, session.session_date), as_dict=True,
        )
        return any(r.course_session == session.name or overlaps(slot.start_time, slot.end_time, r.start_time, r.end_time) for r in rows)
    rows = frappe.db.sql(
        """SELECT a.course_session, w.start_time, w.end_time
        FROM `tabClass Attendance Entry` a
        JOIN `tabCourse Sessions` s ON s.name=a.course_session
        JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
        WHERE a.student=%s AND a.status NOT IN ('Cancelled', 'Leave')
          AND s.status != 'Cancelled' AND s.session_date=%s AND a.name != %s""",
        (student, session.session_date, exclude or ""), as_dict=True,
    )
    return any(r.course_session == session.name or overlaps(slot.start_time, slot.end_time, r.start_time, r.end_time) for r in rows)


def classroom_capacity(slot, lock=False):
    return cint(frappe.db.get_value("Classroom", slot.classroom, "capacity", for_update=lock)) if slot.get("classroom") else 0


def session_is_future(session, slot):
    return session.status == "Scheduled" and datetime.combine(getdate(session.session_date), get_time(slot.start_time)) > now_datetime()


def reserve_regular_place(student, session_id, source_doctype, source_document, enrollment_type, *,
                          validate_business=None, comments=None):
    """Reserve a regular class in the caller's transaction, serializing on Student and Session.

    A caller holding Booking/Card locks must already hold Student. No commit occurs here.
    Business-specific guards run under the same locks before attendance is inserted.
    """
    frappe.db.sql("SELECT name FROM `tabStudent` WHERE name=%s FOR UPDATE", (student,))
    session = frappe.get_doc("Course Sessions", session_id, for_update=True)
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot, for_update=True)
    term = frappe.get_doc("Term", slot.term, for_update=True)
    capacity = classroom_capacity(slot, lock=True)
    if slot.get("status") != "Active" or term.get("status") not in {"Upcoming", "Active"}:
        frappe.throw("This class or term is not open for booking.")
    if not term.get("start_date") or not term.get("end_date") or not (getdate(term.start_date) <= getdate(session.session_date) <= getdate(term.end_date)):
        frappe.throw("This session is outside the term dates.")
    if not slot.get("start_time") or not slot.get("end_time") or not session_is_future(session, slot):
        frappe.throw("This session is no longer available for booking.")
    if getdate(session.session_date).strftime("%A") != slot.get("day_of_week"):
        frappe.throw("This session does not match the class weekday.")
    if not (session.get("teacher_override") or slot.get("teacher")):
        frappe.throw("This session has no teacher assigned.")
    rows = active_rows(session_id, lock=True)
    for row in rows:
        if row.student == student:
            if source_doctype and source_document and row.get("source_doctype") == source_doctype and row.get("source_document") == source_document:
                return row.name
            frappe.throw("This student is already listed for this session.")
    if capacity <= 0:
        frappe.throw("Classroom capacity must be configured before booking.")
    if cint(slot.get("ndis_friendly")):
        from qas_custom.services.ndis_friendly import NDIS_FRIENDLY_CAPACITY
        capacity = min(capacity, NDIS_FRIENDLY_CAPACITY)
    enrollments = frappe.db.sql(
        """SELECT student, enrollment_type FROM `tabEnrollment`
        WHERE weekly_timeslot=%s AND status IN ('Planned', 'Active') FOR UPDATE""",
        (slot.name,), as_dict=True,
    )
    if any(row.student == student for row in enrollments):
        frappe.throw("This student already has an enrollment in this class.")
    planned = {row.student for row in enrollments if row.enrollment_type == "Full-Term"}
    if len(planned | {row.student for row in rows}) >= capacity:
        frappe.throw("This classroom is full. Please choose another session.")
    # Use a current locking read after waiting for Student, never an earlier snapshot.
    if student_has_conflict(student, session, slot, lock=True):
        frappe.throw("This student already has a class at this time.")
    if validate_business:
        validate_business(session, slot, term, rows)
    return create_attendance_entry(
        course_session=session_id, student=student, enrollment_type=enrollment_type,
        source_doctype=source_doctype, source_document=source_document,
        comments=comments, prevent_student_duplicate=False, active_only_source=True,
    )
