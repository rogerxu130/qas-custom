from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import frappe
from frappe.utils import getdate, get_time, now_datetime, today


def _require_parent():
    if frappe.session.user == "Guest":
        frappe.throw("Login required.", frappe.PermissionError)

    parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
    if not parent_name:
        frappe.throw("No parent record is linked to this account.", frappe.PermissionError)

    return frappe.get_cached_doc("Parent", parent_name)


def _get_parent_students(parent_name: str):
    return frappe.get_all(
        "Student",
        filters={"guardian": parent_name},
        fields=["name", "student_name", "age", "status"],
        order_by="student_name asc",
    )


def _validate_student_filter(student: str | None, students: list[dict]) -> str | None:
    if not student:
        return None

    allowed = {row["name"] for row in students}
    if student not in allowed:
        frappe.throw("This student is not linked to the current parent account.", frappe.PermissionError)
    return student


def _get_weekly_timeslot_map(timeslot_ids: list[str]):
    if not timeslot_ids:
        return {}

    rows = frappe.get_all(
        "Weekly Timeslot",
        filters={"name": ["in", timeslot_ids]},
        fields=["name", "course", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time"],
    )
    return {row["name"]: row for row in rows}


def _get_teacher_name_map(teacher_ids: list[str]):
    if not teacher_ids:
        return {}

    rows = frappe.get_all("Teacher", filters={"name": ["in", teacher_ids]}, fields=["name", "teacher_name"])
    return {row["name"]: row.get("teacher_name") or row["name"] for row in rows}


def get_parent_schedule_data(student=None):
    parent = _require_parent()
    students = _get_parent_students(parent.name)
    selected_student = _validate_student_filter(student, students)

    student_names = [selected_student] if selected_student else [row["name"] for row in students]
    if not student_names:
      return {"sessions": []}

    attendance_rows = frappe.get_all(
        "Class Attendance Entry",
        filters={"student": ["in", student_names]},
        fields=["course_session", "student", "status"],
    )

    if not attendance_rows:
        return {"sessions": []}

    attendance_by_session_student = {
        (row["course_session"], row["student"]): row.get("status") for row in attendance_rows
    }
    session_ids = sorted({row["course_session"] for row in attendance_rows})

    session_rows = frappe.get_all(
        "Course Sessions",
        filters={"name": ["in", session_ids], "session_date": [">=", today()]},
        fields=["name", "weekly_timeslot", "session_date", "status"],
        order_by="session_date asc, modified asc",
    )

    timeslot_map = _get_weekly_timeslot_map(
        [row["weekly_timeslot"] for row in session_rows if row.get("weekly_timeslot")]
    )
    teacher_map = _get_teacher_name_map(
        [row["teacher"] for row in timeslot_map.values() if row.get("teacher")]
    )

    sessions = []
    for session_row in session_rows:
        timeslot = timeslot_map.get(session_row.get("weekly_timeslot"))
        if not timeslot:
            continue

        matching_students = [
            row["student"] for row in attendance_rows if row["course_session"] == session_row["name"]
        ]
        for student_name in matching_students:
            if selected_student and student_name != selected_student:
                continue

            sessions.append(
                {
                    "session_id": session_row["name"],
                    "student": student_name,
                    "course": timeslot.get("course"),
                    "day_of_week": timeslot.get("day_of_week"),
                    "session_date": session_row.get("session_date"),
                    "start_time": timeslot.get("start_time"),
                    "end_time": timeslot.get("end_time"),
                    "campus": timeslot.get("campus"),
                    "classroom": timeslot.get("classroom"),
                    "teacher": teacher_map.get(timeslot.get("teacher"), timeslot.get("teacher")),
                    "attendance_status": attendance_by_session_student.get(
                        (session_row["name"], student_name)
                    ),
                }
            )

    sessions.sort(
        key=lambda row: (
            getdate(row.get("session_date")) if row.get("session_date") else getdate(today()),
            row.get("start_time") or "",
        )
    )
    return {"sessions": sessions}


def get_parent_vouchers_data(student=None):
    parent = _require_parent()
    students = _get_parent_students(parent.name)
    selected_student = _validate_student_filter(student, students)

    student_names = [selected_student] if selected_student else [row["name"] for row in students]
    if not student_names:
        return {"vouchers": []}

    vouchers = frappe.get_all(
        "Makeup Voucher",
        filters={"student": ["in", student_names]},
        fields=[
            "name",
            "student",
            "course",
            "original_session",
            "leave_request",
            "status",
            "issue_date",
            "expiry_date",
            "used_on_session",
            "used_date",
        ],
        order_by="issue_date desc, modified desc",
    )

    session_ids = {
        row.get("original_session") for row in vouchers if row.get("original_session")
    } | {row.get("used_on_session") for row in vouchers if row.get("used_on_session")}
    session_ids = {value for value in session_ids if value}

    session_rows = {}
    timeslot_map = {}
    teacher_map = {}
    if session_ids:
        session_list = frappe.get_all(
            "Course Sessions",
            filters={"name": ["in", list(session_ids)]},
            fields=["name", "weekly_timeslot", "session_date", "status"],
        )
        session_rows = {row["name"]: row for row in session_list}
        timeslot_map = _get_weekly_timeslot_map(
            [row["weekly_timeslot"] for row in session_list if row.get("weekly_timeslot")]
        )
        teacher_map = _get_teacher_name_map(
            [row["teacher"] for row in timeslot_map.values() if row.get("teacher")]
        )

    now_dt = now_datetime()
    payload = []
    for voucher in vouchers:
        original_session = session_rows.get(voucher.get("original_session"))
        original_timeslot = (
            timeslot_map.get(original_session.get("weekly_timeslot")) if original_session else None
        )

        leave_start_dt = None
        if original_session and original_session.get("session_date") and original_timeslot and original_timeslot.get("start_time"):
            leave_start_dt = datetime.combine(
                getdate(original_session.get("session_date")),
                get_time(original_timeslot.get("start_time")),
            )

        can_cancel_leave = bool(
            voucher.get("status") == "Valid" and leave_start_dt and leave_start_dt > now_dt
        )
        cancel_reason = None
        if voucher.get("status") == "Valid" and not can_cancel_leave:
            cancel_reason = "Class already started"

        payload.append(
            {
                "voucher_id": voucher.get("name"),
                "student": voucher.get("student"),
                "course": voucher.get("course"),
                "status": voucher.get("status"),
                "issue_date": voucher.get("issue_date"),
                "expiry_date": voucher.get("expiry_date"),
                "used_on_session": voucher.get("used_on_session"),
                "used_date": voucher.get("used_date"),
                "leave_session_date": original_session.get("session_date") if original_session else None,
                "leave_day_of_week": original_timeslot.get("day_of_week") if original_timeslot else None,
                "leave_start_time": original_timeslot.get("start_time") if original_timeslot else None,
                "can_cancel_leave": can_cancel_leave,
                "cancel_leave_disabled_reason": cancel_reason,
                "leave_request": voucher.get("leave_request"),
                "teacher": teacher_map.get(original_timeslot.get("teacher")) if original_timeslot else None,
            }
        )

    return {"vouchers": payload}


def get_parent_invoices_data():
    parent = _require_parent()
    customer = parent.get("customer")
    if not customer:
        return {"invoices": []}

    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"customer": customer, "docstatus": 1},
        fields=[
            "name",
            "posting_date",
            "due_date",
            "grand_total",
            "outstanding_amount",
            "status",
        ],
        order_by="posting_date desc, modified desc",
    )

    payload = []
    for invoice in invoices:
        doc = frappe.get_doc("Sales Invoice", invoice["name"])
        payload.append(
            {
                "invoice_id": doc.name,
                "posting_date": doc.posting_date,
                "due_date": doc.due_date,
                "grand_total": float(doc.grand_total or 0),
                "outstanding_amount": float(doc.outstanding_amount or 0),
                "status": doc.status,
                "items": [
                    {
                        "item_code": item.item_code,
                        "description": item.description,
                        "amount": float(item.amount or 0),
                    }
                    for item in doc.items
                ],
            }
        )

    return {"invoices": payload}
