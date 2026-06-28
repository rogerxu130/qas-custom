from __future__ import annotations

import frappe

from qas_custom.modules.billing.invoice_settings import DEFAULT_INVOICE_SETTINGS, SETTINGS_DOCTYPE
from qas_custom.patches.v2026_06_28_parent_invoice_format import (
	PRINT_FORMAT_NAME,
	_parent_invoice_print_html,
)


def execute():
	_add_sales_invoice_payment_fields()
	_seed_invoice_settings()
	_refresh_parent_invoice_print_format()
	frappe.clear_cache()


def _add_sales_invoice_payment_fields():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return

	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_payment_section",
			"fieldtype": "Section Break",
			"label": "QAS Payment Instructions",
			"insert_after": _existing_field("Sales Invoice", ["billing_note", "remarks", "due_date"]),
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_invoice_message",
			"fieldtype": "Small Text",
			"label": "Invoice Email Message",
			"insert_after": "qas_payment_section",
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_accepted_payment_methods",
			"fieldtype": "Data",
			"label": "Accepted Payment Methods",
			"insert_after": "qas_invoice_message",
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_bank_account_name",
			"fieldtype": "Data",
			"label": "Bank Account Name",
			"insert_after": "qas_accepted_payment_methods",
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_bank_bsb",
			"fieldtype": "Data",
			"label": "BSB",
			"insert_after": "qas_bank_account_name",
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_bank_account_number",
			"fieldtype": "Data",
			"label": "Account Number",
			"insert_after": "qas_bank_bsb",
			"allow_on_submit": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_bank_reference_note",
			"fieldtype": "Small Text",
			"label": "Bank Reference Note",
			"insert_after": "qas_bank_account_number",
			"allow_on_submit": 1,
		},
	)
	frappe.clear_cache(doctype="Sales Invoice")


def _seed_invoice_settings():
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	for fieldname, value in DEFAULT_INVOICE_SETTINGS.items():
		if value and not frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname):
			frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _refresh_parent_invoice_print_format():
	if not frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		return
	frappe.db.set_value(
		"Print Format",
		PRINT_FORMAT_NAME,
		"html",
		_parent_invoice_print_html(),
		update_modified=False,
	)


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


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
