from __future__ import annotations

import frappe


def execute():
	_relax_sales_invoice_item_enrollment()
	frappe.clear_cache(doctype="Sales Invoice Item")
	frappe.clear_cache(doctype="Sales Invoice")


def _relax_sales_invoice_item_enrollment():
	for dt in ("DocField", "Custom Field"):
		filters = (
			{"parent": "Sales Invoice Item", "fieldname": "enrollment"}
			if dt == "DocField"
			else {"dt": "Sales Invoice Item", "fieldname": "enrollment"}
		)
		name = frappe.db.exists(dt, filters)
		if not name:
			continue
		frappe.db.set_value(
			dt,
			name,
			{
				"fieldtype": "Data",
				"options": None,
			},
			update_modified=False,
		)
