from __future__ import annotations

import json

import frappe

from qas_custom.modules.makeup.commands import (
    cancel_parent_leave_request_core,
    get_parent_redeemable_sessions_core,
    redeem_parent_voucher_core,
    submit_parent_leave_request_core,
)
from qas_custom.services.parent_portal_read import (
    _get_parent_students,
    _require_parent,
)


def get_parent_csrf_token_data():
    _require_parent()
    return {"csrf_token": frappe.sessions.get_csrf_token()}


def submit_parent_leave_request_data(student=None, course_session=None):
    payload = _get_request_payload()
    student = student or payload.get("student")
    course_session = course_session or payload.get("course_session")

    parent = _require_parent()
    students = _get_parent_students(parent.name)
    return submit_parent_leave_request_core(
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
