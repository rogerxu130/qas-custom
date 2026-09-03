import frappe

from qas_custom.services.password_reset import (
    confirm_teacher_password_reset,
    request_teacher_password_reset,
    validate_teacher_password_reset_token,
)
from qas_custom.services.parent_classroom_messages import (
    create_teacher_parent_classroom_message_data,
    get_teacher_parent_classroom_messages_data,
    retry_teacher_parent_classroom_message_data,
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
from qas_custom.services.teacher_training import (
    get_teacher_training_article_data,
    get_teacher_training_articles_data,
)
from qas_custom.services.workshops import (
    get_teacher_workshop_session_detail_data,
    get_teacher_workshop_sessions_data,
    get_workshop_photo_content_data,
    get_workshop_video_content_data,
    publish_teacher_workshop_homework_data,
    publish_teacher_workshop_photo_post_data,
    publish_teacher_workshop_video_post_data,
    update_teacher_workshop_attendance_data,
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
def teacher_portal_get_training_articles(limit=120):
    return get_teacher_training_articles_data(limit=limit)


@frappe.whitelist()
def teacher_portal_get_training_article(article=None):
    return get_teacher_training_article_data(article=article)


@frappe.whitelist()
def teacher_portal_update_attendance(course_session=None, updates=None):
    return update_teacher_attendance_data(course_session=course_session, updates=updates)


@frappe.whitelist()
def teacher_portal_create_parent_classroom_message(
    course_session=None,
    attendance_entry=None,
    student=None,
    category=None,
    message=None,
    client_request_id=None,
):
    return create_teacher_parent_classroom_message_data(
        course_session=course_session,
        attendance_entry=attendance_entry,
        student=student,
        category=category,
        message=message,
        client_request_id=client_request_id,
    )


@frappe.whitelist()
def teacher_portal_get_parent_classroom_messages(course_session=None, student=None, limit=50):
    return get_teacher_parent_classroom_messages_data(
        course_session=course_session,
        student=student,
        limit=limit,
    )


@frappe.whitelist()
def teacher_portal_retry_parent_classroom_message(parent_classroom_message=None):
    return retry_teacher_parent_classroom_message_data(parent_classroom_message=parent_classroom_message)


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
def teacher_portal_get_photo(photo_post=None, photo_idx=None, support_token=None):
    payload = get_teacher_photo_content_data(photo_post=photo_post, photo_idx=photo_idx)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.type = "download"


@frappe.whitelist()
def teacher_portal_get_video(video_post=None, download=0, support_token=None):
    payload = get_teacher_video_content_data(video_post=video_post, download=download)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = payload["display_content_as"]
    frappe.local.response.type = "download"


@frappe.whitelist()
def teacher_portal_get_workshop_sessions(from_date=None, to_date=None):
    return get_teacher_workshop_sessions_data(from_date=from_date, to_date=to_date)


@frappe.whitelist()
def teacher_portal_get_workshop_session_detail(workshop_session=None):
    return get_teacher_workshop_session_detail_data(workshop_session=workshop_session)


@frappe.whitelist()
def teacher_portal_update_workshop_attendance(workshop_session=None, updates=None):
    return update_teacher_workshop_attendance_data(workshop_session=workshop_session, updates=updates)


@frappe.whitelist()
def teacher_portal_publish_workshop_homework(workshop_session=None, title=None, description=None):
    return publish_teacher_workshop_homework_data(workshop_session=workshop_session, title=title, description=description)


@frappe.whitelist()
def teacher_portal_publish_workshop_photo_post(workshop_session=None, title=None, caption=None):
    return publish_teacher_workshop_photo_post_data(workshop_session=workshop_session, title=title, caption=caption)


@frappe.whitelist()
def teacher_portal_publish_workshop_video_post(workshop_session=None, title=None, caption=None):
    return publish_teacher_workshop_video_post_data(workshop_session=workshop_session, title=title, caption=caption)


@frappe.whitelist()
def teacher_portal_get_workshop_photo_content(photo_post=None, photo_idx=None, support_token=None):
    payload = get_workshop_photo_content_data(photo_post=photo_post, photo_idx=photo_idx, audience="teacher")
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.type = "download"


@frappe.whitelist()
def teacher_portal_get_workshop_video_content(video_post=None, download=0, support_token=None):
    payload = get_workshop_video_content_data(video_post=video_post, audience="teacher", download=download)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = payload["display_content_as"]
    frappe.local.response.type = "download"
