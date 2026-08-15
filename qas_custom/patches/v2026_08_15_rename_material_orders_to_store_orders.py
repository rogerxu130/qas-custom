from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("Custom Field", {"dt": "Sales Invoice", "fieldname": "qas_invoice_type"}):
		return
	frappe.db.set_value(
		"Custom Field",
		{"dt": "Sales Invoice", "fieldname": "qas_invoice_type"},
		"options",
		"Course\nStore Credit Top-up\nHoliday Program\nStore Order\nOther",
	)
	frappe.clear_cache(doctype="Sales Invoice")
