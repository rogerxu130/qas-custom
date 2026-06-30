from __future__ import annotations

import frappe


def execute():
	_update_invoice_item_session_field()
	_backfill_invoice_item_session_snapshots()
	frappe.clear_cache(doctype="Sales Invoice Item")
	frappe.clear_cache(doctype="Sales Invoice")


def _update_invoice_item_session_field():
	custom_field = frappe.db.get_value(
		"Custom Field",
		{"dt": "Sales Invoice Item", "fieldname": "course_session"},
		"name",
	)
	if custom_field:
		frappe.db.set_value(
			"Custom Field",
			custom_field,
			{
				"fieldtype": "Data",
				"options": "",
				"read_only": 1,
			},
			update_modified=False,
		)
		return

	docfield = frappe.db.get_value(
		"DocField",
		{"parent": "Sales Invoice Item", "fieldname": "course_session"},
		"name",
	)
	if docfield:
		frappe.db.set_value(
			"DocField",
			docfield,
			{
				"fieldtype": "Data",
				"options": "",
				"read_only": 1,
			},
			update_modified=False,
		)


def _backfill_invoice_item_session_snapshots():
	if not frappe.db.has_column("Sales Invoice Item", "course_session"):
		return

	from qas_custom.services.display_labels import get_course_session_snapshot_label

	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"course_session": ["is", "set"]},
		fields=["name", "course_session"],
		limit_page_length=0,
	)
	for row in rows:
		value = row.get("course_session")
		if not value or " · " in value or not frappe.db.exists("Course Sessions", value):
			continue
		snapshot = get_course_session_snapshot_label(value)
		if snapshot and snapshot != value:
			frappe.db.set_value("Sales Invoice Item", row.name, "course_session", snapshot, update_modified=False)
