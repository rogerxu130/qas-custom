from __future__ import annotations

import frappe


FIELDNAME = "qas_apply_store_credit_on_submit"


def execute():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": FIELDNAME,
			"fieldtype": "Check",
			"label": "Apply Store Credit on Submit",
			"insert_after": "qas_amount_payable",
			"default": "0",
		},
	)
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
