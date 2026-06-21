from __future__ import annotations

import json
from datetime import datetime

import frappe
from frappe.utils import add_days, get_time, getdate, now_datetime, today

from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, create_attendance_entry
from qas_custom.services.parent_portal_read import (
    _get_teacher_name_map,
    _get_parent_students,
    _get_weekly_timeslot_map,
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


def get_parent_redeemable_sessions_data(voucher_id=None):
    payload = _get_request_payload()
    voucher_id = voucher_id or payload.get("voucher_id")

    parent = _require_parent()
    voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
    _validate_voucher_available_for_redeem(voucher)

    return {
        "voucher": _build_makeup_voucher_payload(voucher),
        "available_sessions": _get_redeemable_makeup_sessions(voucher),
    }


def redeem_parent_voucher_data(voucher_id=None, session_id=None):
    payload = _get_request_payload()
    voucher_id = voucher_id or payload.get("voucher_id")
    session_id = session_id or payload.get("session_id")

    if not session_id:
        frappe.throw("Please select a makeup session.")

    parent = _require_parent()
    voucher = _get_parent_makeup_voucher(voucher_id, parent.name)

    if voucher.get("status") == "Used" and voucher.get("used_on_session") == session_id:
        attendance_entry = create_attendance_entry(
            course_session=session_id,
            student=voucher.student,
            enrollment_type="Makeup",
            source_doctype="Makeup Voucher",
            source_document=voucher.name,
            comments=f"Added from Makeup Voucher {voucher.name}",
            makeup_voucher=voucher.name,
        )
        return {
            "voucher": _build_makeup_voucher_payload(voucher),
            "attendance_entry": attendance_entry,
            "session": _build_redeem_session_payload(session_id),
        }

    _validate_voucher_available_for_redeem(voucher)
    _validate_session_can_redeem_voucher(voucher, session_id)

    attendance_entry = create_attendance_entry(
        course_session=session_id,
        student=voucher.student,
        enrollment_type="Makeup",
        source_doctype="Makeup Voucher",
        source_document=voucher.name,
        comments=f"Added from Makeup Voucher {voucher.name}",
        makeup_voucher=voucher.name,
        prevent_student_duplicate=True,
    )

    voucher.status = "Used"
    voucher.used_on_session = session_id
    voucher.used_date = today()
    voucher.save(ignore_permissions=True)

    return {
        "voucher": _build_makeup_voucher_payload(voucher),
        "attendance_entry": attendance_entry,
        "session": _build_redeem_session_payload(session_id),
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


def _get_parent_makeup_voucher(voucher_id: str | None, parent_name: str):
    if not voucher_id:
        frappe.throw("Makeup voucher is required.")
    if not frappe.db.exists("Makeup Voucher", voucher_id):
        frappe.throw("Makeup voucher was not found.")

    voucher = frappe.get_doc("Makeup Voucher", voucher_id)
    if not voucher.get("student"):
        frappe.throw("This makeup voucher is missing a student.")

    guardian = frappe.db.get_value("Student", voucher.student, "guardian")
    if guardian != parent_name:
        frappe.throw("This makeup voucher is not linked to the current parent account.", frappe.PermissionError)

    return voucher


def _validate_voucher_available_for_redeem(voucher):
    if voucher.get("status") != "Valid":
        frappe.throw("This makeup voucher is not available for booking.")
    if voucher.get("used_on_session"):
        frappe.throw("This makeup voucher has already been used.")
    if voucher.get("expiry_date") and getdate(voucher.expiry_date) < getdate(today()):
        frappe.throw("This makeup voucher has expired.")


def _get_redeemable_makeup_sessions(voucher):
    session_rows = frappe.get_all(
        "Course Sessions",
        filters={
            "session_date": ["between", [getdate(today()), getdate(add_days(today(), 90))]],
        },
        fields=["name", "weekly_timeslot", "session_date", "status"],
        order_by="session_date asc, modified asc",
        limit=300,
    )
    if not session_rows:
        return []

    timeslot_map = _get_weekly_timeslot_map(
        [row["weekly_timeslot"] for row in session_rows if row.get("weekly_timeslot")]
    )
    teacher_map = _get_teacher_name_map(
        [row["teacher"] for row in timeslot_map.values() if row.get("teacher")]
    )

    sessions = []
    for session in session_rows:
        if session.get("status") == "Cancelled":
            continue
        timeslot = timeslot_map.get(session.get("weekly_timeslot"))
        if not timeslot or not _course_accepts_makeup_voucher(timeslot.get("course"), voucher.get("course")):
            continue
        if frappe.db.exists(
            ATTENDANCE_DOCTYPE,
            {"course_session": session["name"], "student": voucher.student},
        ):
            continue

        sessions.append(
            {
                "session_id": session["name"],
                "course": timeslot.get("course"),
                "session_date": session.get("session_date"),
                "day_of_week": timeslot.get("day_of_week"),
                "start_time": timeslot.get("start_time"),
                "end_time": timeslot.get("end_time"),
                "campus": timeslot.get("campus"),
                "classroom": timeslot.get("classroom"),
                "teacher": teacher_map.get(timeslot.get("teacher"), timeslot.get("teacher")),
            }
        )

    return sessions


def _validate_session_can_redeem_voucher(voucher, session_id: str):
    available_session_ids = {
        row["session_id"] for row in _get_redeemable_makeup_sessions(voucher)
    }
    if session_id not in available_session_ids:
        frappe.throw("This class session is not available for this makeup voucher.")


def _course_accepts_makeup_voucher(target_course: str | None, voucher_course: str | None):
    if not target_course:
        return False
    if target_course == voucher_course:
        return True

    try:
        course_doc = frappe.get_cached_doc("Course", target_course)
    except frappe.DoesNotExistError:
        return False

    if not course_doc.get("is_makeup_course"):
        return False

    accepted_courses = [
        row.get("course")
        for row in course_doc.get("accepted_makeup_course") or []
        if row.get("course")
    ]
    return not accepted_courses or voucher_course in accepted_courses


def _build_makeup_voucher_payload(voucher):
    return {
        "voucher_id": voucher.name,
        "student": voucher.get("student"),
        "course": voucher.get("course"),
        "status": voucher.get("status"),
        "issue_date": voucher.get("issue_date"),
        "expiry_date": voucher.get("expiry_date"),
        "used_on_session": voucher.get("used_on_session"),
        "used_date": voucher.get("used_date"),
        "leave_request": voucher.get("leave_request"),
    }


def _build_redeem_session_payload(session_id: str):
    session = frappe.get_doc("Course Sessions", session_id)
    timeslot = frappe.get_cached_doc("Weekly Timeslot", session.weekly_timeslot) if session.weekly_timeslot else None
    return {
        "session_id": session.name,
        "course": timeslot.course if timeslot else None,
        "session_date": session.session_date,
        "start_time": timeslot.start_time if timeslot else None,
        "end_time": timeslot.end_time if timeslot else None,
        "campus": timeslot.campus if timeslot else None,
        "classroom": timeslot.classroom if timeslot else None,
    }


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
