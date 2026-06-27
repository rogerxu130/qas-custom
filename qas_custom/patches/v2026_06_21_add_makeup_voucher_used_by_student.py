from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Makeup Voucher"):
		return

	if not frappe.db.exists("Custom Field", {"dt": "Makeup Voucher", "fieldname": "used_by_student"}):
		insert_after = "used_date" if frappe.db.exists("DocField", {"parent": "Makeup Voucher", "fieldname": "used_date"}) else "student"
		frappe.get_doc(
			{
				"doctype": "Custom Field",
				"dt": "Makeup Voucher",
				"fieldname": "used_by_student",
				"fieldtype": "Link",
				"label": "Used By Student",
				"options": "Student",
				"insert_after": insert_after,
				"read_only": 1,
				"description": "Student who actually used this family-level makeup voucher.",
			}
		).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Makeup Voucher")

	frappe.db.sql(
		"""
		update `tabMakeup Voucher`
		set used_by_student = student
		where coalesce(used_by_student, '') = ''
		  and coalesce(student, '') != ''
		  and (
		    status = 'Used'
		    or coalesce(used_on_session, '') != ''
		  )
		"""
	)
