import frappe

from qas_custom.services.teacher_portal import (
    get_teacher_me_data,
    get_teacher_session_detail_data,
    get_teacher_sessions_data,
)


@frappe.whitelist()
def teacher_portal_get_me():
    return get_teacher_me_data()


@frappe.whitelist()
def teacher_portal_get_sessions(from_date=None, to_date=None):
    return get_teacher_sessions_data(from_date=from_date, to_date=to_date)


@frappe.whitelist()
def teacher_portal_get_session_detail(course_session=None):
    return get_teacher_session_detail_data(course_session=course_session)
