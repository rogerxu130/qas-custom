import frappe
from qas_custom.services.activity import get_activity


@frappe.whitelist()
def school_admin_get_activity(doctype=None, name=None, offset=0):
    return get_activity(doctype=doctype, name=name, offset=offset)
