from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Notification"):
		return
	meta = frappe.get_meta("Notification")
	if not meta.has_field("document_type"):
		return

	fields = ["name"]
	for fieldname in ["enabled", "disabled"]:
		if meta.has_field(fieldname):
			fields.append(fieldname)

	rows = frappe.get_all(
		"Notification",
		filters={"document_type": "Sales Invoice"},
		fields=fields,
		limit_page_length=0,
	)
	for row in rows:
		values = {}
		if meta.has_field("enabled"):
			values["enabled"] = 0
		if meta.has_field("disabled"):
			values["disabled"] = 1
		if values:
			frappe.db.set_value("Notification", row.name, values, update_modified=False)
