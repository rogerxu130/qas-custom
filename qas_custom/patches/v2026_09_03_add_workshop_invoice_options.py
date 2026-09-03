from __future__ import annotations

import frappe


def execute():
	_add_option("Sales Invoice", "qas_invoice_type", "Workshop")
	_add_option("Sales Invoice Item", "qas_line_type", "Workshop")
	frappe.clear_cache(doctype="Sales Invoice")
	frappe.clear_cache(doctype="Sales Invoice Item")


def _add_option(dt, fieldname, option):
	rows = frappe.get_all("Custom Field", filters={"dt":dt, "fieldname":fieldname}, fields=["name", "options"], limit=1)
	if not rows:
		return
	options = [value for value in (rows[0].options or "").splitlines() if value]
	if option not in options:
		options.append(option)
		frappe.db.set_value("Custom Field", rows[0].name, "options", "\n".join(options), update_modified=False)
