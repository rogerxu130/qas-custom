from __future__ import annotations

import frappe


def execute():
	for doctype in ("Store Product Image", "Store Product Video"):
		if not frappe.db.exists("DocType", doctype):
			continue
		for row in frappe.get_all(doctype, fields=["name", "idx", "display_order"], limit_page_length=0):
			frappe.db.set_value(doctype, row.name, "display_order", max(0, int(row.idx or 1) - 1), update_modified=False)
