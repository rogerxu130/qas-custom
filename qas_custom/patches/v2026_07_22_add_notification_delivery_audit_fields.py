from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Notification Log"):
		return

	fields = [
		{"fieldname": "recipient_email", "fieldtype": "Data", "label": "Recipient Email", "insert_after": "event_key", "read_only": 1},
		{"fieldname": "delivery_status", "fieldtype": "Select", "label": "Delivery Status", "options": "Queued\nSent\nFailed", "insert_after": "recipient_email", "read_only": 1},
		{"fieldname": "failure_reason", "fieldtype": "Small Text", "label": "Failure Reason", "insert_after": "delivery_status", "read_only": 1},
		{"fieldname": "sent_at", "fieldtype": "Datetime", "label": "Sent At", "insert_after": "failure_reason", "read_only": 1},
	]
	for values in fields:
		_ensure_custom_field("Notification Log", values)
	frappe.clear_cache(doctype="Notification Log")


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
		return
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return
	doc = frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values})
	doc.insert(ignore_permissions=True)
