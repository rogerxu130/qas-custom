from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, nowdate, get_url

from qas_custom.modules.common import has_field, set_if_field


SETTINGS_DOCTYPE = "QAS Invoice Settings"

DEFAULT_INVOICE_SETTINGS = {
	"school_name": "Queensland Art School",
	"school_logo": "/assets/qas_custom/images/qas-logo.png",
	"legal_name": "",
	"abn": "",
	"school_email": "",
	"school_phone": "",
	"school_website": "",
	"school_address": "",
	"payment_due_days": 7,
	"invoice_message": "Thank you for learning with Queensland Art School. Please contact us if you have any questions about this invoice.",
	"accepted_payment_methods": "Bank transfer, cash, or POS",
	"bank_account_name": "",
	"bank_bsb": "",
	"bank_account_number": "",
	"bank_reference_note": "For bank transfers, please use the invoice number as the reference.",
	"store_credit_liability_account": "",
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
	if not settings_doctype_available():
		return _with_public_logo_url(settings)

	try:
		doc = frappe.get_single(SETTINGS_DOCTYPE)
	except (KeyError, ImportError, frappe.DoesNotExistError):
		return _with_public_logo_url(settings)
	for fieldname in settings:
		value = doc.get(fieldname)
		if fieldname == "payment_due_days":
			settings[fieldname] = _normalize_due_days(value)
		elif value:
			settings[fieldname] = value
	return _with_public_logo_url(settings)


def update_invoice_settings(payload):
	if not settings_doctype_available():
		frappe.throw(f"{SETTINGS_DOCTYPE} is not installed yet.")

	doc = frappe.get_single(SETTINGS_DOCTYPE)
	for fieldname in DEFAULT_INVOICE_SETTINGS:
		if fieldname in payload:
			if fieldname == "payment_due_days":
				doc.set(fieldname, _normalize_due_days(payload.get(fieldname)))
			else:
				doc.set(fieldname, (payload.get(fieldname) or "").strip())
	doc.save(ignore_permissions=True)
	return get_invoice_settings()


def get_public_school_branding():
	settings = get_invoice_settings()
	return {
		"school_name": settings.get("school_name") or DEFAULT_INVOICE_SETTINGS["school_name"],
		"school_logo": settings.get("school_logo") or "",
		"school_logo_url": settings.get("school_logo_url") or "",
	}


def settings_doctype_available():
	try:
		return bool(frappe.db.exists("DocType", SETTINGS_DOCTYPE))
	except (KeyError, ImportError, frappe.DoesNotExistError):
		return False


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


def get_default_invoice_due_date(posting_date: str | None = None):
	settings = get_invoice_settings()
	base_date = posting_date or nowdate()
	return add_days(base_date, _normalize_due_days(settings.get("payment_due_days")))


def apply_default_invoice_dates(invoice_doc, *, force: bool = False):
	posting_date = invoice_doc.get("posting_date") or nowdate()
	changed = False
	if force or not invoice_doc.get("posting_date"):
		invoice_doc.posting_date = posting_date
		changed = True
	if force or not invoice_doc.get("due_date"):
		invoice_doc.due_date = get_default_invoice_due_date(posting_date)
		changed = True
	for row in invoice_doc.get("payment_schedule", []):
		if force or not row.get("due_date"):
			row.due_date = invoice_doc.due_date
			changed = True
	return changed


def _with_public_logo_url(settings):
	settings["school_logo_url"] = _public_asset_url(settings.get("school_logo"))
	return settings


def _public_asset_url(value):
	path = str(value or "").strip()
	if not path:
		return ""
	if path.startswith(("http://", "https://")):
		return path
	if not path.startswith("/"):
		path = f"/files/{path}"
	return get_url(path)


def _normalize_due_days(value):
	days = cint(value if value is not None else DEFAULT_INVOICE_SETTINGS["payment_due_days"])
	return max(days, 0)
