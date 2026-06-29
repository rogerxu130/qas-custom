from __future__ import annotations

import frappe

from qas_custom.modules.billing.invoice_settings import SETTINGS_DOCTYPE


def execute():
	_add_payment_due_days_field()
	if frappe.db.exists("DocType", SETTINGS_DOCTYPE) and not frappe.db.get_single_value(SETTINGS_DOCTYPE, "payment_due_days"):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "payment_due_days", 7)
	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)


def _add_payment_due_days_field():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	if frappe.db.exists("DocField", {"parent": SETTINGS_DOCTYPE, "fieldname": "payment_due_days"}):
		return
	if frappe.db.exists("Custom Field", {"dt": SETTINGS_DOCTYPE, "fieldname": "payment_due_days"}):
		return
	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": SETTINGS_DOCTYPE,
			"fieldname": "payment_due_days",
			"fieldtype": "Int",
			"label": "Default Payment Due Days",
			"default": "7",
			"insert_after": "bank_reference_note",
		}
	).insert(ignore_permissions=True)
