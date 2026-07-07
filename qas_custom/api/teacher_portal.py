import frappe

from qas_custom.services.password_reset import (
    confirm_teacher_password_reset,
    request_teacher_password_reset,
    validate_teacher_password_reset_token,
)
from qas_custom.services.teacher_portal import (
    get_teacher_csrf_token_data,
    get_teacher_me_data,
    get_teacher_photo_content_data,
    get_teacher_session_detail_data,
    get_teacher_sessions_data,
    get_teacher_video_content_data,
    publish_teacher_homework_data,
    publish_teacher_photo_post_data,
    publish_teacher_video_post_data,
    update_teacher_attendance_data,
)


@frappe.whitelist(allow_guest=True)
def teacher_portal_request_password_reset(email=None):
    return request_teacher_password_reset(email)


@frappe.whitelist(allow_guest=True)
def teacher_portal_validate_password_reset_token(token=None):
    return validate_teacher_password_reset_token(token)


@frappe.whitelist(allow_guest=True)
def teacher_portal_confirm_password_reset(token=None, new_password=None):
    return confirm_teacher_password_reset(token, new_password)


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


@frappe.whitelist()
def teacher_portal_publish_video_post(course_session=None, title=None, caption=None):
    return publish_teacher_video_post_data(
        course_session=course_session,
        title=title,
        caption=caption,
    )


@frappe.whitelist()
def teacher_portal_get_photo(photo_post=None, photo_idx=None):
    payload = get_teacher_photo_content_data(photo_post=photo_post, photo_idx=photo_idx)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.type = "download"


@frappe.whitelist()
def teacher_portal_get_video(video_post=None, download=0):
    payload = get_teacher_video_content_data(video_post=video_post, download=download)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = payload["display_content_as"]
    frappe.local.response.type = "download"
