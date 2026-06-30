from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Course"):
		return

	for role in ("School Admin", "System Manager"):
		permission = frappe.db.get_value(
			"DocPerm",
			{"parent": "Course", "role": role, "permlevel": 0},
			"name",
		)
		if permission:
			frappe.db.set_value("DocPerm", permission, "import", 1, update_modified=False)

	frappe.clear_cache(doctype="Course")
