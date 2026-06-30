from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Course"):
		return
	frappe.db.set_value("DocType", "Course", "allow_import", 1, update_modified=False)
	frappe.clear_cache(doctype="Course")
