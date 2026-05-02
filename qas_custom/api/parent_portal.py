import frappe

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
