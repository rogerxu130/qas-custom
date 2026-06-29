from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import frappe
from frappe.utils import getdate, get_time, now_datetime, today

from qas_custom.modules.billing.invoice_settings import get_invoice_payment_context
from qas_custom.modules.billing.store_credit import get_invoice_store_credit_applied
from qas_custom.modules.course_schedule.queries import (
    get_teacher_name_map as _get_teacher_name_map,
    get_weekly_timeslot_map as _get_weekly_timeslot_map,
)
from qas_custom.modules.notifications import parent_portal_invoice_link
from qas_custom.services.display_labels import get_makeup_voucher_label, get_student_display_name


def _require_parent():
    if frappe.session.user == "Guest":
        frappe.throw("Login required.", frappe.PermissionError)

    parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
    if not parent_name:
        frappe.throw("No parent record is linked to this account.", frappe.PermissionError)

    return frappe.get_cached_doc("Parent", parent_name)


def _get_parent_students(parent_name: str):
    fields = ["name", "student_name", "age", "status"]
    if frappe.db.has_column("Student", "student_code"):
        fields.append("student_code")
    return frappe.get_all(
        "Student",
        filters={"guardian": parent_name},
        fields=fields,
        order_by="student_name asc",
    )


def _validate_student_filter(student: str | None, students: list[dict]) -> str | None:
    if not student:
        return None

    allowed = {row["name"] for row in students}
    if student not in allowed:
        frappe.throw("This student is not linked to the current parent account.", frappe.PermissionError)
    return student


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

    voucher_fields = [
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
    ]
    if frappe.db.has_column("Makeup Voucher", "used_by_student"):
        voucher_fields.append("used_by_student")
    if frappe.db.has_column("Makeup Voucher", "voucher_label"):
        voucher_fields.append("voucher_label")

    vouchers = frappe.get_all(
        "Makeup Voucher",
        filters={"student": ["in", student_names]},
        fields=voucher_fields,
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
                "voucher_label": get_makeup_voucher_label(voucher),
                "student": voucher.get("student"),
                "student_display": get_student_display_name(voucher.get("student")),
                "course": voucher.get("course"),
                "status": voucher.get("status"),
                "issue_date": voucher.get("issue_date"),
                "expiry_date": voucher.get("expiry_date"),
                "used_on_session": voucher.get("used_on_session"),
                "used_date": voucher.get("used_date"),
                "used_by_student": voucher.get("used_by_student"),
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
        store_credit_applied = float(get_invoice_store_credit_applied(doc.name) or 0)
        payable_amount = max(0, float(doc.outstanding_amount or doc.grand_total or 0) - store_credit_applied)
        payment_status = "Paid" if payable_amount <= 0 else (doc.status or "Unpaid")
        payload.append(
            {
                "invoice_id": doc.name,
                "posting_date": doc.posting_date,
                "due_date": doc.due_date,
                "grand_total": float(doc.grand_total or 0),
                "outstanding_amount": float(doc.outstanding_amount or 0),
                "store_credit_applied": store_credit_applied,
                "payable_amount": payable_amount,
                "invoice_link": parent_portal_invoice_link(doc.name),
                "payment_link": parent_portal_invoice_link(doc.name),
                "payment_status": payment_status,
                "status": doc.status,
                **get_invoice_payment_context(doc),
                "items": [
                    {
                        "item_code": item.item_code,
                        "description": item.description,
                        "amount": float(item.amount or 0),
                        "student": item.get("student"),
                        "student_display_name": item.get("student_display_name"),
                        "enrollment": item.get("enrollment"),
                        "course": item.get("course"),
                        "term": item.get("term"),
                        "course_session": item.get("course_session"),
                        "session_count": item.get("session_count"),
                        "qas_line_type": item.get("qas_line_type"),
                    }
                    for item in doc.items
                ],
            }
        )

    return {"invoices": payload}
