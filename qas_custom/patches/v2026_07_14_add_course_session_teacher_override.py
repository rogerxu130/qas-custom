from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Course Sessions"):
		return
	if frappe.db.exists("Custom Field", {"dt": "Course Sessions", "fieldname": "teacher_override"}):
		return
	if frappe.db.exists("DocField", {"parent": "Course Sessions", "fieldname": "teacher_override"}):
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Course Sessions",
			"fieldname": "teacher_override",
			"fieldtype": "Link",
			"label": "Teacher Override",
			"options": "Teacher",
			"insert_after": "status",
			"description": "Optional one-session teacher. Leave blank to use the Weekly Timeslot teacher.",
		}
	).insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Course Sessions")
