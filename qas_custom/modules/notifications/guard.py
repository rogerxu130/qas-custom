from __future__ import annotations

import json
import quopri

import frappe


LEGACY_INVOICE_SUBJECT_MARKERS = (
	"New Invoice",
	"Queensland Art School - New Invoice",
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
		"status": ("in", ["Not Sent", "Partially Sent", "Error"]),
	}

	rows = frappe.get_all(
		"Email Queue",
		filters=filters,
		fields=["name", "message", "reference_doctype", "reference_name", "status", "attachments"],
		limit_page_length=0,
	)
	deleted = 0
	for row in rows:
		if invoice_name and not _email_queue_mentions_invoice(row, invoice_name):
			continue
		if not _is_legacy_invoice_email_queue(row):
			continue
		frappe.db.delete("Email Queue Recipient", {"parent": row.name})
		frappe.db.delete("Email Queue", {"name": row.name})
		deleted += 1
	return deleted


def suppress_legacy_invoice_email_queue(doc=None, method=None):
	if not doc:
		return

	if not _is_legacy_invoice_email_queue(doc):
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
	has_sales_invoice_name = "acc-sinv-" in normalized_text or (
		bool(invoice_name) and invoice_name.lower() in normalized_text
	)
	if invoice_name and f"New Invoice {invoice_name}".lower() in normalized_text:
		return True
	if has_sales_invoice_name and any(marker.lower() in normalized_text for marker in LEGACY_INVOICE_SUBJECT_MARKERS):
		return True
	return has_sales_invoice_name and any(
		marker.lower() in normalized_text for marker in LEGACY_INVOICE_MESSAGE_MARKERS
	)


def _is_legacy_invoice_email_queue(row) -> bool:
	message = row.get("message")
	reference_doctype = row.get("reference_doctype")
	reference_name = row.get("reference_name")
	attachments = row.get("attachments")

	if _is_legacy_invoice_email_message(message, reference_name):
		return True
	if _has_legacy_sales_invoice_mime_attachment(message):
		return True
	if reference_doctype == "Sales Invoice" and _has_sales_invoice_print_attachment(attachments):
		return True
	return False


def _email_queue_mentions_invoice(row, invoice_name: str) -> bool:
	if row.get("reference_name") == invoice_name:
		return True
	return invoice_name.lower() in _decoded_text(row.get("message")).lower()


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


def _has_sales_invoice_print_attachment(attachments) -> bool:
	if not attachments:
		return False
	if isinstance(attachments, str):
		try:
			attachments = json.loads(attachments)
		except Exception:
			return "print_format_attachment" in attachments and "Sales Invoice" in attachments
	for attachment in attachments or []:
		if not isinstance(attachment, dict):
			continue
		if attachment.get("print_format_attachment") == 1 and attachment.get("doctype") == "Sales Invoice":
			return True
	return False


def _has_legacy_sales_invoice_mime_attachment(message) -> bool:
	text = _decoded_text(message).lower()
	if not text:
		return False
	return (
		"content-type: application/pdf" in text
		and "content-disposition: attachment" in text
		and "acc-sinv-" in text
		and any(marker.lower() in text for marker in LEGACY_INVOICE_SUBJECT_MARKERS)
	)
