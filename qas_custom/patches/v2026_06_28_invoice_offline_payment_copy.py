from __future__ import annotations

import frappe

from qas_custom.patches.v2026_06_28_parent_invoice_format import (
	PRINT_FORMAT_NAME,
	_parent_invoice_print_html,
)


def execute():
	if not frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		return

	frappe.db.set_value(
		"Print Format",
		PRINT_FORMAT_NAME,
		"html",
		_parent_invoice_print_html(),
		update_modified=False,
	)
	frappe.clear_cache(doctype="Sales Invoice")
