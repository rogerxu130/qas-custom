from __future__ import annotations

import quopri

import frappe


LEGACY_INVOICE_SUBJECT_MARKERS = (
	"New Invoice",
)

LEGACY_INVOICE_MESSAGE_MARKERS = (
	"A new invoice has been generated for your account",
	"Please log in to your Parent Portal",
)


def disable_sales_invoice_auto_notifications(doc=None, method=None):
	if not frappe.db.exists("DocType", "Notification"):
		return
	meta = frappe.get_meta("Notification")
	if doc is not None and getattr(doc, "flags", None):
		doc.flags.notifications = None
		doc.flags.notifications_executed = []

	_clear_sales_invoice_notification_cache()

	fields = ["name"]
	for fieldname in ["document_type", "subject", "message", "enabled", "disabled"]:
		if meta.has_field(fieldname):
			fields.append(fieldname)

	rows = frappe.get_all("Notification", fields=fields, limit_page_length=0)

	for row in rows:
		if not _is_sales_invoice_or_legacy_invoice_notification(row):
			continue

		values = {}
		if meta.has_field("enabled") and row.get("enabled") != 0:
			values["enabled"] = 0
		if meta.has_field("disabled") and row.get("disabled") != 1:
			values["disabled"] = 1
		if values:
			frappe.db.set_value("Notification", row.name, values, update_modified=False)

	_clear_sales_invoice_notification_cache()
	if doc is not None and getattr(doc, "flags", None):
		doc.flags.notifications = None
		doc.flags.notifications_executed = []


def purge_legacy_invoice_email_queue(doc=None, method=None, invoice: str | None = None):
	invoice_name = invoice or getattr(doc, "name", None)
	if not frappe.db.exists("DocType", "Email Queue"):
		return 0

	filters = {
		"reference_doctype": "Sales Invoice",
		"status": ("in", ["Not Sent", "Partially Sent", "Error"]),
	}
	if invoice_name:
		filters["reference_name"] = invoice_name

	rows = frappe.get_all(
		"Email Queue",
		filters=filters,
		fields=["name", "message", "reference_name", "status"],
		limit_page_length=0,
	)
	deleted = 0
	for row in rows:
		if not _is_legacy_invoice_email_message(row.get("message"), row.get("reference_name")):
			continue
		frappe.db.delete("Email Queue Recipient", {"parent": row.name})
		frappe.db.delete("Email Queue", {"name": row.name})
		deleted += 1
	return deleted


def suppress_legacy_invoice_email_queue(doc=None, method=None):
	if not doc or getattr(doc, "reference_doctype", None) != "Sales Invoice":
		return

	if not _is_legacy_invoice_email_message(doc.get("message"), doc.get("reference_name")):
		return

	doc.status = "Sent"
	doc.error = "Suppressed by QAS Custom: legacy Sales Invoice notification replaced by School Admin invoice email."
	for recipient in doc.get("recipients") or []:
		recipient.status = "Sent"


def _is_sales_invoice_or_legacy_invoice_notification(row) -> bool:
	if row.get("document_type") == "Sales Invoice":
		return True

	subject = _as_text(row.get("subject"))
	message = _as_text(row.get("message"))
	if any(marker in subject for marker in LEGACY_INVOICE_SUBJECT_MARKERS):
		return True
	return any(marker in message for marker in LEGACY_INVOICE_MESSAGE_MARKERS)


def _is_legacy_invoice_email_message(message, invoice_name: str | None = None) -> bool:
	text = _decoded_text(message)
	if not text:
		return False
	normalized_text = text.lower()
	if invoice_name and f"New Invoice {invoice_name}".lower() in normalized_text:
		return True
	if "Queensland Art School - New Invoice".lower() in normalized_text:
		return True
	return any(marker.lower() in normalized_text for marker in LEGACY_INVOICE_MESSAGE_MARKERS)


def _clear_sales_invoice_notification_cache():
	frappe.cache.hdel("notifications", "Sales Invoice")


def _as_text(value) -> str:
	if value is None:
		return ""
	return str(value)


def _decoded_text(value) -> str:
	text = _as_text(value)
	if not text:
		return ""
	try:
		decoded = quopri.decodestring(text.encode("utf-8", errors="ignore")).decode("utf-8", errors="ignore")
	except Exception:
		decoded = ""
	return f"{text}\n{decoded}"
