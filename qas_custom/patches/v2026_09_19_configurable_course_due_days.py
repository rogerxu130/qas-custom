"""Initialize new settings only; never rewrite invoices or existing fallback days."""
import frappe


def execute():
	for field, default in (("course_due_lead_days", 7), ("course_due_grace_days", 3)):
		value = frappe.db.get_single_value("QAS Invoice Settings", field)
		if value is None or value == "":
			frappe.db.set_single_value("QAS Invoice Settings", field, default)
