from __future__ import annotations

import json
from datetime import datetime

import frappe
from frappe.utils import get_time, getdate, now_datetime

from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.parent_portal_read import (
    _get_parent_students,
    _require_parent,
    _validate_student_filter,
)


def get_parent_csrf_token_data():
    _require_parent()
    return {"csrf_token": frappe.sessions.get_csrf_token()}


def submit_parent_leave_request_data(student=None, course_session=None):
    payload = _get_request_payload()
    student = student or payload.get("student")
    course_session = course_session or payload.get("course_session")

    if not student:
        frappe.throw("Please select a student.")
    if not course_session:
        frappe.throw("Please select a class session.")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    selected_student = _validate_student_filter(student, students)

    session_doc, attendance_row, timeslot = _get_leave_session(
        student=selected_student,
        course_session=course_session,
    )
    _validate_no_active_leave(student=selected_student, course_session=session_doc.name)

    leave_request = frappe.new_doc("Leave Request")
    leave_request.student = selected_student
    leave_request.course_session = session_doc.name
    leave_request.weekly_timeslot = session_doc.weekly_timeslot
    leave_request.course = timeslot.course
    leave_request.session_date = session_doc.session_date
    leave_request.status = "Approved"
    leave_request.flags.ignore_permissions = True
    leave_request.insert()
    leave_request.reload()

    return {
        "leave_request": leave_request.name,
        "makeup_voucher": leave_request.get("makeup_voucher"),
        "session": {
            "session_id": session_doc.name,
            "student": selected_student,
            "course": timeslot.course,
            "session_date": session_doc.session_date,
            "day_of_week": timeslot.day_of_week,
            "start_time": timeslot.start_time,
            "end_time": timeslot.end_time,
            "campus": timeslot.campus,
            "classroom": timeslot.classroom,
            "attendance_status": "Leave",
        },
    }


def _get_request_payload():
    request = getattr(frappe.local, "request", None)
    if request:
        json_payload = request.get_json(silent=True)
        if json_payload:
            return json_payload

    if frappe.form_dict:
        return dict(frappe.form_dict)

    data = getattr(request, "data", None)
    if data:
        try:
            return json.loads(data)
        except (TypeError, ValueError):
            return {}

    return {}


def _get_leave_session(student: str, course_session: str):
    session_doc = frappe.get_doc("Course Sessions", course_session)
    attendance_row = frappe.db.get_value(
        ATTENDANCE_DOCTYPE,
        {"course_session": course_session, "student": student},
        ["name", "status"],
        as_dict=True,
    )
    if not attendance_row:
        frappe.throw("This student is not listed in the selected class session.", frappe.PermissionError)

    if attendance_row.status != "To be started":
        frappe.throw("This class session is not available for leave.")

    if not session_doc.weekly_timeslot:
        frappe.throw("The selected class session is missing a weekly timeslot.")

    timeslot = frappe.get_cached_doc("Weekly Timeslot", session_doc.weekly_timeslot)
    if not timeslot.course:
        frappe.throw("The selected class session is missing a course.")

    if _get_session_start(session_doc, timeslot) <= now_datetime():
        frappe.throw("This class has already started.")

    return session_doc, attendance_row, timeslot


def _get_session_start(session_doc, timeslot):
    if not session_doc.session_date or not timeslot.start_time:
        frappe.throw("The selected class session is missing date or time.")

    return datetime.combine(
        getdate(session_doc.session_date),
        get_time(timeslot.start_time),
    )


def _validate_no_active_leave(student: str, course_session: str):
    existing_leave = frappe.db.get_value(
        "Leave Request",
        {
            "student": student,
            "course_session": course_session,
            "status": "Approved",
        },
        ["name", "makeup_voucher"],
        as_dict=True,
    )
    if existing_leave:
        frappe.throw("A leave request already exists for this class session.")

    existing_voucher = frappe.db.exists(
        "Makeup Voucher",
        {
            "student": student,
            "original_session": course_session,
            "status": ["in", ["Valid", "Used"]],
        },
    )
    if existing_voucher:
        frappe.throw("A makeup voucher already exists for this class session.")
