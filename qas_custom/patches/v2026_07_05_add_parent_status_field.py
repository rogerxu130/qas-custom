from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Parent"):
		return

	_ensure_custom_field(
		"Parent",
		{
			"fieldname": "status",
			"fieldtype": "Select",
			"label": "Status",
			"options": "Active\nInactive",
			"default": "Active",
			"insert_after": _existing_field("Parent", ["linked_user", "mobile_number", "parent_name"]),
			"in_list_view": 1,
			"in_standard_filter": 1,
		},
	)
	_backfill_parent_status()
	frappe.clear_cache(doctype="Parent")


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


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return

	name = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
	if name:
		doc = frappe.get_doc("Custom Field", name)
		changed = False
		for key, value in values.items():
			if doc.get(key) != value:
				doc.set(key, value)
				changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return

	frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values}).insert(ignore_permissions=True)


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
