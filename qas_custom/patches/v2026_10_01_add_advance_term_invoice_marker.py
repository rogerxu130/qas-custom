import frappe

from qas_custom.patches.v2026_07_20_add_manual_invoice_store_credit_choice import _ensure_custom_field


def execute():
	_ensure_custom_field("Sales Invoice", {
		"fieldname": "qas_advance_term_invoice",
		"fieldtype": "Check",
		"label": "Contains advance term tuition",
		"insert_after": "due_date",
		"default": "0",
		"read_only": 1,
		"no_copy": 0,
	})
	frappe.clear_cache(doctype="Sales Invoice")
