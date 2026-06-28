from __future__ import annotations

import frappe

from qas_custom.modules.common import has_field, set_if_field


SETTINGS_DOCTYPE = "QAS Invoice Settings"

DEFAULT_INVOICE_SETTINGS = {
	"invoice_message": "Thank you for learning with Queensland Art School. Please contact us if you have any questions about this invoice.",
	"accepted_payment_methods": "Bank transfer, cash, or POS",
	"bank_account_name": "",
	"bank_bsb": "",
	"bank_account_number": "",
	"bank_reference_note": "For bank transfers, please use the invoice number as the reference.",
}

SNAPSHOT_FIELD_MAP = {
	"invoice_message": "qas_invoice_message",
	"accepted_payment_methods": "qas_accepted_payment_methods",
	"bank_account_name": "qas_bank_account_name",
	"bank_bsb": "qas_bank_bsb",
	"bank_account_number": "qas_bank_account_number",
	"bank_reference_note": "qas_bank_reference_note",
}


def get_invoice_settings():
	settings = dict(DEFAULT_INVOICE_SETTINGS)
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return settings

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	for fieldname in settings:
		value = doc.get(fieldname)
		if value:
			settings[fieldname] = value
	return settings


def update_invoice_settings(payload):
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		frappe.throw(f"{SETTINGS_DOCTYPE} is not installed yet.")

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	for fieldname in DEFAULT_INVOICE_SETTINGS:
		if fieldname in payload:
			doc.set(fieldname, (payload.get(fieldname) or "").strip())
	doc.save(ignore_permissions=True)
	return get_invoice_settings()


def apply_invoice_payment_snapshot(invoice_doc, *, force: bool = False):
	settings = get_invoice_settings()
	changed = False
	for source_field, target_field in SNAPSHOT_FIELD_MAP.items():
		if not has_field("Sales Invoice", target_field):
			continue
		if force or not invoice_doc.get(target_field):
			set_if_field(invoice_doc, target_field, settings.get(source_field))
			changed = True
	return changed


def get_invoice_payment_context(invoice_doc):
	settings = get_invoice_settings()
	context = {}
	for source_field, target_field in SNAPSHOT_FIELD_MAP.items():
		value = invoice_doc.get(target_field) if has_field("Sales Invoice", target_field) else None
		context[source_field] = value or settings.get(source_field) or ""
	return context
