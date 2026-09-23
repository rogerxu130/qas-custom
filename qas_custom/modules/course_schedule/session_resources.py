"""Shared course-session booking resources."""
from datetime import datetime

import frappe
from frappe.utils import cint, getdate, get_time, now_datetime


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
