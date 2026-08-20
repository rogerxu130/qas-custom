from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Parent"):
		return

	_ensure_custom_field(
		"Parent",
		{
			"fieldname": "mass_email_unsubscribed",
			"fieldtype": "Check",
			"label": "Unsubscribed from mass emails",
			"description": "Stops announcements, workshops, and deal emails only.",
			"default": "0",
			"insert_after": _existing_field("Parent", ["status", "linked_user", "mobile_number", "parent_name"]),
		},
	)
	frappe.clear_cache(doctype="Parent")


def _ensure_custom_field(doctype: str, values: dict):
	fieldname = values["fieldname"]
	if frappe.db.exists("DocField", {"parent": doctype, "fieldname": fieldname}):
		return

	name = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname})
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

	frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **values}).insert(ignore_permissions=True)


def _existing_field(doctype: str, fieldnames: list[str]) -> str:
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": doctype, "fieldname": fieldname}):
			return fieldname
		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
			return fieldname
	return ""
