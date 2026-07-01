from __future__ import annotations

import frappe


def disable_sales_invoice_auto_notifications(doc=None, method=None):
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
		if meta.has_field("enabled") and row.get("enabled") != 0:
			values["enabled"] = 0
		if meta.has_field("disabled") and row.get("disabled") != 1:
			values["disabled"] = 1
		if values:
			frappe.db.set_value("Notification", row.name, values, update_modified=False)
