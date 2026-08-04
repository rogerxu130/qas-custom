from __future__ import annotations

import json

import frappe

from qas_custom.modules.makeup.commands import (
    cancel_parent_leave_request_core,
    complete_parent_leave_and_keep_voucher_core,
    complete_parent_leave_and_redeem_core,
    get_parent_leave_makeup_options_core,
    get_parent_redeemable_sessions_core,
    redeem_parent_voucher_core,
)
from qas_custom.services.parent_portal_read import (
    _get_parent_students,
    _require_parent,
    _validate_student_filter,
)
from qas_custom.services.support_view import reject_support_view_write


def get_parent_csrf_token_data():
    _require_parent()
    return {"csrf_token": frappe.sessions.get_csrf_token()}


def update_parent_student_teaching_notes_data(student=None, teaching_notes=None):
    reject_support_view_write()
    parent = _require_parent()
    students = _get_parent_students(parent.name)
    student = _validate_student_filter(str(student or "").strip(), students)
    if not student:
        frappe.throw("Student is required.")
    if not frappe.db.has_column("Student", "teaching_notes"):
        frappe.throw("Student special-needs notes are not available on this site. Please run migrate.")

    notes = str(teaching_notes or "").strip()
    if len(notes) > 80:
        frappe.throw("Special needs / important classroom notes must be 80 characters or fewer.")

    doc = frappe.get_doc("Student", student)
    doc.teaching_notes = notes
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"student": doc.name, "teaching_notes": notes}


def submit_parent_leave_request_data(student=None, course_session=None):
    reject_support_view_write()
    frappe.throw(
        "Please choose a makeup time or keep a voucher before confirming leave."
    )


def get_parent_leave_makeup_options_data(student=None, course_session=None, redeem_student=None, campus=None):
    payload = _get_request_payload()
    student = student or payload.get("student")
    course_session = course_session or payload.get("course_session")
    redeem_student = redeem_student or payload.get("redeem_student") or payload.get("use_for_student")
    campus = campus or payload.get("campus")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return get_parent_leave_makeup_options_core(
        parent=parent,
        students=students,
        student=student,
        course_session=course_session,
        redeem_student=redeem_student,
        campus=campus,
    )


def complete_parent_leave_and_redeem_data(
    student=None,
    course_session=None,
    session_id=None,
    redeem_student=None,
):
    reject_support_view_write()
    payload = _get_request_payload()
    student = student or payload.get("student")
    course_session = course_session or payload.get("course_session")
    session_id = session_id or payload.get("session_id")
    redeem_student = redeem_student or payload.get("redeem_student") or payload.get("use_for_student")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return complete_parent_leave_and_redeem_core(
        parent=parent,
        students=students,
        student=student,
        course_session=course_session,
        session_id=session_id,
        redeem_student=redeem_student,
    )


def complete_parent_leave_and_keep_voucher_data(student=None, course_session=None):
    reject_support_view_write()
    payload = _get_request_payload()
    student = student or payload.get("student")
    course_session = course_session or payload.get("course_session")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return complete_parent_leave_and_keep_voucher_core(
        parent=parent,
        students=students,
        student=student,
        course_session=course_session,
    )


def get_parent_redeemable_sessions_data(voucher_id=None, student=None):
    payload = _get_request_payload()
    voucher_id = voucher_id or payload.get("voucher_id")
    student = student or payload.get("student") or payload.get("redeem_student")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return get_parent_redeemable_sessions_core(
        parent=parent,
        students=students,
        voucher_id=voucher_id,
        student=student,
    )


def redeem_parent_voucher_data(voucher_id=None, session_id=None, student=None):
    reject_support_view_write()
    payload = _get_request_payload()
    voucher_id = voucher_id or payload.get("voucher_id")
    session_id = session_id or payload.get("session_id")
    student = student or payload.get("student") or payload.get("redeem_student")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return redeem_parent_voucher_core(
        parent=parent,
        students=students,
        voucher_id=voucher_id,
        session_id=session_id,
        student=student,
    )


def cancel_parent_leave_data(voucher_id=None):
    reject_support_view_write()
    payload = _get_request_payload()
    voucher_id = voucher_id or payload.get("voucher_id")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return cancel_parent_leave_request_core(
        parent=parent,
        students=students,
        voucher_id=voucher_id,
    )


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
