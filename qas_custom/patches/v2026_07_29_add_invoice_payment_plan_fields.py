from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	fields = [
		{"fieldname": "qas_payment_plan_section", "fieldtype": "Section Break", "label": "QAS Payment Plan", "insert_after": "qas_bank_reference_note", "allow_on_submit": 1},
		{"fieldname": "qas_has_payment_plan", "fieldtype": "Check", "label": "Has Payment Plan", "insert_after": "qas_payment_plan_section", "allow_on_submit": 1},
		{"fieldname": "qas_payment_plan_status", "fieldtype": "Select", "label": "Payment Plan Status", "options": "\nActive\nCompleted\nCancelled", "insert_after": "qas_has_payment_plan", "read_only": 1, "allow_on_submit": 1},
		{"fieldname": "qas_payment_plan_installments", "fieldtype": "Table", "label": "Payment Plan Installments", "options": "QAS Invoice Payment Plan Installment", "insert_after": "qas_payment_plan_status", "allow_on_submit": 1},
		{"fieldname": "qas_payment_plan_created_by", "fieldtype": "Link", "label": "Payment Plan Created By", "options": "User", "insert_after": "qas_payment_plan_installments", "read_only": 1, "allow_on_submit": 1},
		{"fieldname": "qas_payment_plan_created_at", "fieldtype": "Datetime", "label": "Payment Plan Created At", "insert_after": "qas_payment_plan_created_by", "read_only": 1, "allow_on_submit": 1},
	]
	for values in fields:
		_ensure_custom_field("Sales Invoice", values)
	frappe.clear_cache(doctype="Sales Invoice")


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
