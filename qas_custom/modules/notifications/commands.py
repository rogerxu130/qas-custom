from __future__ import annotations

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


DEFAULT_PARENT_PORTAL_URL = "https://portal.queenslandartschool.com"


def send_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
	notification_log: str | None = None,
):
	recipient = _invoice_recipient(invoice_doc)
	event_key = f"invoice_{event}:{invoice_doc.name}"
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _("Queensland Art School invoice {0}").format(invoice_doc.name)
	message = _invoice_email_message(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)

	log_name = notification_log or _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}

	try:
		frappe.sendmail(
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=invoice_doc.name,
		)
		_mark_notification_sent(log_name)
		return {
			"sent": True,
			"recipient": recipient["email"],
			"notification_log": log_name,
			"payment_link": payment_link,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS invoice notification failed: {invoice_doc.name}")
		_mark_notification_failed(log_name, "Email send failed.")
		_add_invoice_comment(invoice_doc.name, _("Invoice notification failed for {0}.").format(recipient["email"]))
		return {
			"sent": False,
			"recipient": recipient["email"],
			"reason": "Email send failed.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}


def enqueue_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
):
	recipient = _invoice_recipient(invoice_doc)
	event_key = f"invoice_{event}:{invoice_doc.name}"
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _("Queensland Art School invoice {0}").format(invoice_doc.name)
	message = _invoice_email_message(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)
	log_name = _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"queued": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}

	_mark_notification_queued(log_name)
	frappe.enqueue(
		"qas_custom.modules.notifications.commands.send_parent_invoice_notification_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		invoice=invoice_doc.name,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		notification_log=log_name,
	)
	return {
		"sent": False,
		"queued": True,
		"recipient": recipient["email"],
		"notification_log": log_name,
		"payment_link": payment_link,
	}


def send_parent_invoice_notification_job(
	invoice: str,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
	notification_log: str | None = None,
):
	invoice_doc = frappe.get_doc("Sales Invoice", invoice)
	return send_parent_invoice_notification(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		notification_log=notification_log,
	)


def parent_portal_invoice_link(invoice: str):
	base_url = (
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or DEFAULT_PARENT_PORTAL_URL
	)
	return f"{str(base_url).rstrip('/')}/invoices?{urlencode({'invoice': invoice})}"


def get_invoice_notification_summary(invoice: str):
	if not _notification_log_available() or not invoice:
		return {"count": 0, "latest": None}

	rows = frappe.get_all(
		"Notification Log",
		filters={"document_type": "Sales Invoice", "document_name": invoice},
		fields=_safe_notification_fields(),
		order_by="creation desc",
		limit=5,
	)
	return {
		"count": len(rows),
		"latest": _notification_payload(rows[0]) if rows else None,
		"items": [_notification_payload(row) for row in rows],
	}


def _invoice_recipient(invoice_doc):
	parent = invoice_doc.get("parent")
	linked_user = None
	email = None

	if parent:
		fields = ["name"]
		for fieldname in ["linked_user", "email", "email_id", "contact_email"]:
			if frappe.db.has_column("Parent", fieldname):
				fields.append(fieldname)
		parent_info = frappe.db.get_value("Parent", parent, fields, as_dict=True) or {}
		linked_user = parent_info.get("linked_user")
		email = _first_value(parent_info, ["email", "email_id", "contact_email"])

	if not email:
		for fieldname in ["contact_email", "email", "email_id"]:
			if frappe.db.has_column("Sales Invoice", fieldname) and invoice_doc.get(fieldname):
				email = invoice_doc.get(fieldname)
				break

	if not email and invoice_doc.customer:
		fields = []
		for fieldname in ["email_id", "email", "contact_email"]:
			if frappe.db.has_column("Customer", fieldname):
				fields.append(fieldname)
		if fields:
			customer_info = frappe.db.get_value("Customer", invoice_doc.customer, fields, as_dict=True) or {}
			email = _first_value(customer_info, fields)

	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user

	return {"email": email, "for_user": linked_user, "parent": parent, "customer": invoice_doc.customer}


def _invoice_email_message(invoice_doc, event, store_credit_applied, payable_amount, payment_link):
	action = "is ready" if event == "approved" else "has been resent"
	payment_line = (
		_("No payment is required because this invoice is fully covered.")
		if flt(payable_amount) <= 0
		else _("Please review the invoice and payment details in the Parent Portal.")
	)
	return """
		<p>Hi,</p>
		<p>Your Queensland Art School invoice <strong>{invoice}</strong> {action}.</p>
		<p>Total: <strong>${total:.2f}</strong><br>
		Store credit applied: <strong>${credit:.2f}</strong><br>
		Amount payable: <strong>${payable:.2f}</strong><br>
		Due date: <strong>{due_date}</strong></p>
		<p>{payment_line}</p>
		<p><a href="{payment_link}">View invoice in Parent Portal</a></p>
	""".format(
		invoice=invoice_doc.name,
		action=action,
		total=flt(invoice_doc.grand_total),
		credit=flt(store_credit_applied),
		payable=flt(payable_amount),
		due_date=invoice_doc.get("due_date") or "-",
		payment_line=payment_line,
		payment_link=payment_link,
	)


def _create_notification_log(event_key, recipient, subject, message, document_type, document_name):
	if not _notification_log_available():
		return None

	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.type = "Alert"
	log.email_content = message
	log.document_type = document_type
	log.document_name = document_name
	log.from_user = frappe.session.user
	if log.meta.has_field("for_user") and recipient.get("for_user"):
		log.for_user = recipient.get("for_user")
	elif log.meta.has_field("for_user"):
		log.for_user = frappe.session.user
	_set_if_field(log, "event_key", event_key)
	_set_if_field(log, "email_to", recipient.get("email"))
	_set_if_field(log, "recipient_email", recipient.get("email"))
	_set_if_field(log, "reference_doctype", document_type)
	_set_if_field(log, "reference_name", document_name)
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	return log.name


def _mark_notification_sent(log_name):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Sent"
	if meta.has_field("sent_at"):
		values["sent_at"] = now_datetime()
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _mark_notification_queued(log_name):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Queued"
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _mark_notification_failed(log_name, reason):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Failed"
	for fieldname in ["failure_reason", "error", "error_message"]:
		if meta.has_field(fieldname):
			values[fieldname] = reason
			break
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _notification_log_available():
	return bool(frappe.db.exists("DocType", "Notification Log"))


def _safe_notification_fields():
	meta = frappe.get_meta("Notification Log")
	fields = ["name", "subject", "creation", "document_type", "document_name"]
	for fieldname in ["for_user", "email_to", "recipient_email", "status", "delivery_status", "email_status", "failure_reason", "error_message"]:
		if meta.has_field(fieldname):
			fields.append(fieldname)
	return fields


def _notification_payload(row):
	status = row.get("delivery_status") or row.get("email_status") or row.get("status") or "Logged"
	return {
		"name": row.get("name"),
		"subject": row.get("subject"),
		"creation": row.get("creation"),
		"recipient": row.get("email_to") or row.get("recipient_email") or row.get("for_user"),
		"status": status,
		"failure_reason": row.get("failure_reason") or row.get("error_message"),
	}


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _first_value(mapping, fieldnames):
	for fieldname in fieldnames:
		value = mapping.get(fieldname)
		if value:
			return value
	return None


def _add_invoice_comment(invoice, message):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
			"content": message,
		}
	).insert(ignore_permissions=True)
