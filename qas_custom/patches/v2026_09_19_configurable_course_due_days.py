"""Initialize new settings only; never rewrite invoices or existing fallback days."""
import frappe


def execute():
	for field, default in (("course_due_lead_days", 7), ("course_due_grace_days", 3)):
		value = frappe.db.get_value("Singles", {"doctype": "QAS Invoice Settings", "field": field}, "value")
		if value is None or value == "":
			frappe.db.set_single_value("QAS Invoice Settings", field, default)
