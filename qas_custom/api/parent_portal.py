import frappe

from qas_custom.services.parent_feed import get_parent_feed_data
from qas_custom.services.parent_info import get_parent_info_data
from qas_custom.services.parent_portal_read import (
    get_parent_invoices_data,
    get_parent_schedule_data,
    get_parent_vouchers_data,
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
def parent_portal_get_info():
    return get_parent_info_data()


@frappe.whitelist()
def parent_portal_get_schedule(student=None):
    return get_parent_schedule_data(student=student)


@frappe.whitelist()
def parent_portal_get_vouchers(student=None):
    return get_parent_vouchers_data(student=student)


@frappe.whitelist()
def parent_portal_get_invoices():
    return get_parent_invoices_data()
