from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Student"):
		return

	if not frappe.db.exists("Custom Field", {"dt": "Student", "fieldname": "enable_adhoc_portal"}):
		insert_after = "status" if frappe.db.exists("DocField", {"parent": "Student", "fieldname": "status"}) else None
		frappe.get_doc(
			{
				"doctype": "Custom Field",
				"dt": "Student",
				"fieldname": "enable_adhoc_portal",
				"fieldtype": "Check",
				"label": "Enable Adhoc Portal",
				"insert_after": insert_after,
				"description": "Allow this student to be booked through the Adhoc / Pay-as-you-go portal.",
			}
		).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Student")
