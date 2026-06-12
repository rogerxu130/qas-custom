import frappe

from qas_custom.services.teacher_portal import (
    get_teacher_csrf_token_data,
    get_teacher_me_data,
    get_teacher_session_detail_data,
    get_teacher_sessions_data,
    publish_teacher_homework_data,
    publish_teacher_photo_post_data,
    update_teacher_attendance_data,
)


@frappe.whitelist()
def teacher_portal_get_me():
    return get_teacher_me_data()


@frappe.whitelist()
def teacher_portal_get_csrf_token():
    return get_teacher_csrf_token_data()


@frappe.whitelist()
def teacher_portal_get_sessions(from_date=None, to_date=None):
    return get_teacher_sessions_data(from_date=from_date, to_date=to_date)


@frappe.whitelist()
def teacher_portal_get_session_detail(course_session=None):
    return get_teacher_session_detail_data(course_session=course_session)


@frappe.whitelist()
def teacher_portal_update_attendance(course_session=None, updates=None):
    return update_teacher_attendance_data(course_session=course_session, updates=updates)


@frappe.whitelist()
def teacher_portal_publish_homework(course_session=None, title=None, description=None):
    return publish_teacher_homework_data(
        course_session=course_session,
        title=title,
        description=description,
    )


@frappe.whitelist()
def teacher_portal_publish_photo_post(course_session=None, title=None, caption=None):
    return publish_teacher_photo_post_data(
        course_session=course_session,
        title=title,
        caption=caption,
    )
