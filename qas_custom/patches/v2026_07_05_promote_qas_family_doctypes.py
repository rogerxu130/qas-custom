from __future__ import annotations

import frappe


def execute():
	_remove_parent_status_custom_field()
	_backfill_parent_status()
	for doctype in ("Parent", "Student", "Accepted Course"):
		if frappe.db.exists("DocType", doctype):
			frappe.clear_cache(doctype=doctype)


def _remove_parent_status_custom_field():
	if not frappe.db.exists("DocField", {"parent": "Parent", "fieldname": "status"}):
		return

	for name in frappe.get_all("Custom Field", filters={"dt": "Parent", "fieldname": "status"}, pluck="name"):
		frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)


def _backfill_parent_status():
	if not frappe.db.has_column("Parent", "status"):
		return

	frappe.db.sql(
		"""
		update `tabParent`
		set status = 'Active'
		where status is null or status = ''
		"""
	)
