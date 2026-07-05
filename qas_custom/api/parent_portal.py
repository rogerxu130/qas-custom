import frappe

from qas_custom.services.announcements import get_parent_announcements_data
from qas_custom.services.parent_feed import (
    get_parent_feed_data,
    get_parent_feed_photo_content,
    get_parent_feed_video_content,
)
from qas_custom.services.parent_info import get_parent_info_data
from qas_custom.services.parent_portal_read import (
    get_parent_invoices_data,
    get_parent_schedule_data,
    get_parent_vouchers_data,
)
from qas_custom.services.parent_portal_write import (
    cancel_parent_leave_data,
    get_parent_csrf_token_data,
    get_parent_redeemable_sessions_data,
    redeem_parent_voucher_data,
    submit_parent_leave_request_data,
)
from qas_custom.services.password_reset import (
    confirm_password_reset,
    request_password_reset,
    validate_password_reset_token,
)


@frappe.whitelist(allow_guest=True)
def ping():
    return {
        "ok": True,
        "app": "qas_custom",
        "module": "parent_portal",
        "message": "Parent portal API is reachable.",
    }


def _not_implemented_response(endpoint_name: str):
    return {
        "ok": False,
        "endpoint": endpoint_name,
        "message": "Endpoint scaffold is ready, but business logic is not implemented yet.",
    }


@frappe.whitelist(allow_guest=True)
def parent_portal_request_password_reset(email=None):
    return request_password_reset(email)


@frappe.whitelist(allow_guest=True)
def parent_portal_validate_password_reset_token(token=None):
    return validate_password_reset_token(token)


@frappe.whitelist(allow_guest=True)
def parent_portal_confirm_password_reset(token=None, new_password=None):
    return confirm_password_reset(token, new_password)


@frappe.whitelist()
def get_parent_feed(student=None, page=1, page_length=10):
    return get_parent_feed_data(student=student, page=page, page_length=page_length)


@frappe.whitelist()
def parent_portal_get_feed_photo(photo_post=None, photo_idx=None):
    payload = get_parent_feed_photo_content(photo_post=photo_post, photo_idx=photo_idx)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.type = "download"


@frappe.whitelist()
def parent_portal_get_feed_video(video_post=None, download=0):
    payload = get_parent_feed_video_content(video_post=video_post, download=download)
    frappe.local.response.filename = payload["filename"]
    frappe.local.response.filecontent = payload["content"]
    frappe.local.response.content_type = payload["content_type"]
    frappe.local.response.display_content_as = payload["display_content_as"]
    frappe.local.response.type = "download"


@frappe.whitelist()
def parent_portal_get_info():
    return get_parent_info_data()


@frappe.whitelist()
def parent_portal_get_schedule(student=None):
    return get_parent_schedule_data(student=student)


@frappe.whitelist()
def parent_portal_get_vouchers(student=None):
    return get_parent_vouchers_data(student=student)


@frappe.whitelist()
def parent_portal_get_redeemable_sessions(voucher_id=None, student=None):
    return get_parent_redeemable_sessions_data(voucher_id=voucher_id, student=student)


@frappe.whitelist()
def parent_portal_redeem_voucher(voucher_id=None, session_id=None, student=None):
    return redeem_parent_voucher_data(voucher_id=voucher_id, session_id=session_id, student=student)


@frappe.whitelist()
def parent_portal_get_invoices():
    return get_parent_invoices_data()


@frappe.whitelist()
def parent_portal_get_announcements(limit=30):
    return get_parent_announcements_data(limit=limit)


@frappe.whitelist()
def parent_portal_get_csrf_token():
    return get_parent_csrf_token_data()


@frappe.whitelist()
def parent_portal_leave_request(student=None, course_session=None):
    return submit_parent_leave_request_data(student=student, course_session=course_session)


@frappe.whitelist()
def parent_portal_cancel_leave(voucher_id=None):
    return cancel_parent_leave_data(voucher_id=voucher_id)
