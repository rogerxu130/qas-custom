import frappe

from qas_custom.services.support_view import get_support_view_context


@frappe.whitelist(allow_guest=True)
def support_view_get_context(target_type=None, token=None):
	return get_support_view_context(target_type=target_type, token=token)
