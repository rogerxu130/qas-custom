from __future__ import annotations

import frappe

from qas_custom.patches.v2026_06_28_add_invoice_payment_settings import _ensure_custom_field


def execute():
	if not frappe.db.exists("DocType", "Class Attendance Entry"):
		return

	_ensure_custom_field(
		"Class Attendance Entry",
		{
			"fieldname": "qas_first_class_after_transfer",
			"fieldtype": "Check",
			"label": "First Class After Transfer",
			"insert_after": "enrollment_type",
			"read_only": 1,
			"default": "0",
		},
	)
	frappe.clear_cache(doctype="Class Attendance Entry")
