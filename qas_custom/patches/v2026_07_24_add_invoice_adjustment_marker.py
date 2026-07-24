from __future__ import annotations

import frappe

from qas_custom.patches.v2026_06_28_parent_invoice_format import (
	_set_default_sales_invoice_print_format,
	_upsert_parent_invoice_print_format,
)
from qas_custom.patches.v2026_07_20_add_manual_invoice_store_credit_choice import _ensure_custom_field


def execute():
	if not frappe.db.exists("DocType", "Sales Taxes and Charges"):
		return
	_ensure_custom_field(
		"Sales Taxes and Charges",
		{
			"fieldname": "qas_is_invoice_adjustment",
			"fieldtype": "Check",
			"label": "QAS Invoice Adjustment",
			"insert_after": "description",
			"default": "0",
			"hidden": 1,
			"read_only": 1,
		},
	)
	_upsert_parent_invoice_print_format()
	_set_default_sales_invoice_print_format()
	frappe.clear_cache(doctype="Sales Taxes and Charges")
	frappe.clear_cache(doctype="Sales Invoice")
