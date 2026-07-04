from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Weekly Timeslot"):
		return

	permission = frappe.db.get_value(
		"DocPerm",
		{"parent": "Weekly Timeslot", "role": "System Manager", "permlevel": 0},
		"name",
	)
	if permission:
		frappe.db.set_value("DocPerm", permission, "import", 1, update_modified=False)

	frappe.clear_cache(doctype="Weekly Timeslot")
