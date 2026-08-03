from __future__ import annotations

import frappe

from qas_custom.patches.v2026_06_28_add_invoice_payment_settings import _ensure_custom_field
from qas_custom.patches.v2026_06_28_parent_invoice_format import (
	PRINT_FORMAT_NAME,
	_parent_invoice_print_html,
)


def execute():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return

	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_additional_description",
			"fieldtype": "Long Text",
			"label": "Additional Description",
			"insert_after": "qas_invoice_message",
			"allow_on_submit": 0,
		},
	)
	_refresh_parent_invoice_print_format()
	frappe.clear_cache(doctype="Sales Invoice")


def _refresh_parent_invoice_print_format():
	if not frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		return
	frappe.db.set_value(
		"Print Format",
		PRINT_FORMAT_NAME,
		"html",
		_parent_invoice_print_html(),
		update_modified=False,
	)
