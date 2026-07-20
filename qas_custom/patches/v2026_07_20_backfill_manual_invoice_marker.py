from __future__ import annotations

import frappe

from qas_custom.patches.v2026_07_20_add_manual_invoice_marker import _backfill_manual_invoices


def execute():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	_backfill_manual_invoices()
	frappe.clear_cache(doctype="Sales Invoice")
