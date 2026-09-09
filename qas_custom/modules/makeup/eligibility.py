"""Parent self-service requires an existing regular or trial class roster."""
import frappe


def has_regular_or_trial_student(rows):
    return any(
        row.get("student")
        and row.get("enrollment_type") in {"Full-Term", "Trial", "Pay-as-you-go"}
        and row.get("status") not in {"Cancelled", "Leave"}
        and row.get("source_doctype") != "Makeup Voucher"
        and not row.get("makeup_voucher")
        for row in rows
    )


def sessions_with_regular_or_trial_students(session_ids):
    if not session_ids:
        return set()
    rows = frappe.get_all(
        "Class Attendance Entry",
        filters={"course_session": ["in", list(session_ids)],
                 "status": ["not in", ["Cancelled", "Leave"]],
                 "enrollment_type": ["in", ["Full-Term", "Trial", "Pay-as-you-go"]]},
        fields=["course_session", "student", "enrollment_type", "status", "source_doctype", "makeup_voucher"],
        limit_page_length=0,
    )
    return {row.get("course_session") for row in rows if has_regular_or_trial_student([row])}


def validate_parent_session_roster(session_id):
    # Current locked read after lock_booking: a previously displayed option may now be empty.
    rows = frappe.db.sql(
        """SELECT student, enrollment_type, status, source_doctype, makeup_voucher
        FROM `tabClass Attendance Entry` WHERE course_session=%s FOR UPDATE""",
        (session_id,), as_dict=True,
    )
    if not has_regular_or_trial_student(rows):
        frappe.throw("This session has no regular or trial students. Please choose another session or contact the school.")
