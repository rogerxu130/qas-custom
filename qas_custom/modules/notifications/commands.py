from __future__ import annotations

from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, formatdate, now_datetime
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf

from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts
from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.presentation import build_parent_invoice_context, parent_portal_invoice_link
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


TRIAL_CLASS_REMINDER_EVENT_PREFIX = "trial_class_reminder:"
SESSION_STAFF_NOTIFICATION_EVENT_PREFIX = "session_staff:"
SESSION_STAFF_NOTIFICATION_EVENTS = {
	"leave_requested",
	"makeup_booked",
	"trial_added",
	"trial_cancelled",
	"trial_rescheduled",
}
TRIAL_NOTIFICATION_EVENTS = {"trial_added", "trial_cancelled", "trial_rescheduled"}
TRIAL_ADDED_NOTIFICATION_CONFIG = "qas_trial_added_notification_enabled"
TRIAL_ADDED_NOTIFICATION_DISABLED_REASON = "Trial teacher notifications are disabled by site config."


def send_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
	notification_log: str | None = None,
):
	amounts = _invoice_notification_amounts(invoice_doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	store_credit_applied = amounts["store_credit_applied"]
	payable_amount = amounts["payable_amount"]
	recipient = _invoice_recipient(invoice_doc)
	event_key = _invoice_notification_event_key(invoice_doc, event)
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _invoice_email_subject(invoice_doc, event)
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
		mail_result = sendmail_or_skip(
			action="parent_invoice_notification",
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=invoice_doc.name,
			delayed=False,
			attachments=[
				_invoice_pdf_attachment(
					invoice_doc.name,
					store_credit_applied=store_credit_applied,
					payable_amount=payable_amount,
				)
			],
		)
		if mail_result and mail_result.get("skipped"):
			_mark_notification_failed(log_name, mail_result.get("reason") or email_block_reason())
			return {
				"sent": False,
				"skipped": True,
				"recipient": recipient["email"],
				"reason": mail_result.get("reason") or email_block_reason(),
				"notification_log": log_name,
				"payment_link": payment_link,
			}
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
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
):
	amounts = _invoice_notification_amounts(invoice_doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	store_credit_applied = amounts["store_credit_applied"]
	payable_amount = amounts["payable_amount"]
	recipient = _invoice_recipient(invoice_doc)
	event_key = _invoice_notification_event_key(invoice_doc, event)
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _invoice_email_subject(invoice_doc, event)
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
	if not outbound_email_enabled():
		_mark_notification_failed(log_name, email_block_reason())
		return {
			"sent": False,
			"queued": False,
			"skipped": True,
			"recipient": recipient["email"],
			"reason": email_block_reason(),
			"notification_log": log_name,
			"payment_link": payment_link,
		}

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
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
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


def send_parent_invoice_cancellation_notification(invoice_doc, *, reason=None, notification_log=None):
	recipient = _invoice_recipient(invoice_doc)
	subject = _("Queensland Art School - Invoice cancelled {0}").format(invoice_doc.name)
	message = _invoice_cancellation_email_message(invoice_doc, reason=reason)
	log_name = notification_log or _create_notification_log(
		event_key=f"invoice_cancelled:{invoice_doc.name}",
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)
	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {"sent": False, "reason": "No parent email found.", "notification_log": log_name}
	try:
		mail_result = sendmail_or_skip(
			action="parent_invoice_cancellation_notification",
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=invoice_doc.name,
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason_text = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(log_name, reason_text)
			return {"sent": False, "skipped": True, "recipient": recipient["email"], "reason": reason_text, "notification_log": log_name}
		_mark_notification_sent(log_name)
		return {"sent": True, "recipient": recipient["email"], "notification_log": log_name}
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS invoice cancellation notification failed: {invoice_doc.name}")
		_mark_notification_failed(log_name, "Email send failed.")
		_add_invoice_comment(invoice_doc.name, _("Invoice cancellation notification failed for {0}.").format(recipient["email"]))
		return {"sent": False, "recipient": recipient["email"], "reason": "Email send failed.", "notification_log": log_name}


def enqueue_parent_invoice_cancellation_notification(invoice_doc, *, reason=None):
	recipient = _invoice_recipient(invoice_doc)
	subject = _("Queensland Art School - Invoice cancelled {0}").format(invoice_doc.name)
	message = _invoice_cancellation_email_message(invoice_doc, reason=reason)
	log_name = _create_notification_log(
		event_key=f"invoice_cancelled:{invoice_doc.name}",
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)
	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {"sent": False, "queued": False, "reason": "No parent email found.", "notification_log": log_name}
	_mark_notification_queued(log_name)
	if not outbound_email_enabled():
		_mark_notification_failed(log_name, email_block_reason())
		return {"sent": False, "queued": False, "skipped": True, "recipient": recipient["email"], "reason": email_block_reason(), "notification_log": log_name}
	frappe.enqueue(
		"qas_custom.modules.notifications.commands.send_parent_invoice_cancellation_notification_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		invoice=invoice_doc.name,
		reason=reason,
		notification_log=log_name,
	)
	return {"sent": False, "queued": True, "recipient": recipient["email"], "notification_log": log_name}


def send_parent_invoice_cancellation_notification_job(invoice: str, *, reason=None, notification_log=None):
	return send_parent_invoice_cancellation_notification(
		frappe.get_doc("Sales Invoice", invoice),
		reason=reason,
		notification_log=notification_log,
	)


def maybe_send_parent_invoice_paid_receipt(invoice_doc, *, payment_entry=None, source: str | None = None):
	doc, amounts, result = _prepare_paid_receipt(invoice_doc)
	if result:
		return result
	return send_parent_payment_receipt(doc, payment_entry=payment_entry, source=source, amounts=amounts)


def enqueue_parent_invoice_paid_receipt(invoice_doc, *, payment_entry=None, source: str | None = None):
	"""Queue a paid-invoice receipt without delaying the financial transaction."""
	doc, amounts, result = _prepare_paid_receipt(invoice_doc)
	if result:
		return result

	recipient = _invoice_recipient(doc)
	payment_doc = _payment_entry_doc(payment_entry)
	payment_context = _receipt_payment_context(doc, payment_doc, amounts, source=source)
	event_key = _paid_receipt_event_key(doc.name)
	subject = _receipt_email_subject(doc)
	message = _receipt_email_message(doc, amounts, payment_context)
	log_name = _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"queued": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"receipt": True,
		}

	_mark_notification_queued(log_name)
	if not outbound_email_enabled():
		_mark_notification_failed(log_name, email_block_reason())
		return {
			"sent": False,
			"queued": False,
			"skipped": True,
			"recipient": recipient["email"],
			"reason": email_block_reason(),
			"notification_log": log_name,
			"receipt": True,
		}

	frappe.enqueue(
		"qas_custom.modules.notifications.commands.send_parent_payment_receipt_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		invoice=doc.name,
		payment_entry=payment_doc.name if payment_doc else None,
		source=source,
		notification_log=log_name,
	)
	return {
		"sent": False,
		"queued": True,
		"recipient": recipient["email"],
		"notification_log": log_name,
		"receipt": True,
	}


def send_parent_payment_receipt_job(invoice: str, *, payment_entry=None, source: str | None = None, notification_log: str | None = None):
	doc = frappe.get_doc("Sales Invoice", invoice)
	if int(doc.get("docstatus") or 0) != 1:
		_mark_notification_failed(notification_log, "Invoice is no longer submitted.")
		return {
			"sent": False,
			"skipped": True,
			"reason": "Invoice is no longer submitted.",
			"notification_log": notification_log,
			"receipt": True,
		}
	return send_parent_payment_receipt(
		doc,
		payment_entry=payment_entry,
		source=source,
		notification_log=notification_log,
	)


def send_parent_payment_receipt(invoice_doc, *, payment_entry=None, source: str | None = None, amounts=None, notification_log: str | None = None):
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	amounts = amounts or _invoice_notification_amounts(doc)
	recipient = _invoice_recipient(doc)
	payment_doc = _payment_entry_doc(payment_entry)
	payment_context = _receipt_payment_context(doc, payment_doc, amounts, source=source)
	event_key = _paid_receipt_event_key(doc.name)
	subject = _receipt_email_subject(doc)
	message = _receipt_email_message(doc, amounts, payment_context)

	log_name = notification_log or _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"receipt": True,
		}

	try:
		mail_result = sendmail_or_skip(
			action="parent_payment_receipt",
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=doc.name,
			delayed=False,
			attachments=[_receipt_pdf_attachment(doc.name, payment_entry=payment_doc, amounts=amounts, payment_context=payment_context)],
		)
		if mail_result and mail_result.get("skipped"):
			_mark_notification_failed(log_name, mail_result.get("reason") or email_block_reason())
			return {
				"sent": False,
				"skipped": True,
				"recipient": recipient["email"],
				"reason": mail_result.get("reason") or email_block_reason(),
				"notification_log": log_name,
				"receipt": True,
			}
		_mark_notification_sent(log_name)
		_add_invoice_comment(doc.name, _("Payment receipt sent to {0}.").format(recipient["email"]))
		return {
			"sent": True,
			"recipient": recipient["email"],
			"notification_log": log_name,
			"receipt": True,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS receipt notification failed: {doc.name}")
		_mark_notification_failed(log_name, "Receipt email send failed.")
		_add_invoice_comment(doc.name, _("Payment receipt notification failed for {0}.").format(recipient["email"]))
		return {
			"sent": False,
			"recipient": recipient["email"],
			"reason": "Receipt email send failed.",
			"notification_log": log_name,
			"receipt": True,
		}


def _prepare_paid_receipt(invoice_doc):
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	if not doc or int(doc.get("docstatus") or 0) != 1:
		return doc, None, {"sent": False, "skipped": True, "reason": "Invoice is not submitted."}

	amounts = _invoice_notification_amounts(doc)
	outstanding = _invoice_outstanding_amount(doc)
	payable_amount = flt(amounts.get("payable_amount"))
	if outstanding > 0.005 and payable_amount > 0.005:
		return doc, amounts, {
			"sent": False,
			"skipped": True,
			"reason": "Invoice is not fully paid.",
			"outstanding_amount": outstanding,
			"payable_amount": payable_amount,
		}

	if _paid_receipt_already_logged(doc.name):
		return doc, amounts, {"sent": False, "skipped": True, "duplicate": True, "reason": "Receipt already sent for this invoice."}

	return doc, amounts, None


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


def send_trial_class_reminder(inquiry_doc):
	"""Send one explicit, repeatable reminder for a scheduled trial lesson."""
	doc = frappe.get_doc("Inquiry", inquiry_doc) if isinstance(inquiry_doc, str) else inquiry_doc
	if doc.get("inquiry_type") != "Trial Lesson":
		frappe.throw(_("Trial class reminders are only available for Trial Lesson inquiries."))

	context = _trial_class_reminder_context(doc)
	event_key = "{0}{1}:{2}".format(
		TRIAL_CLASS_REMINDER_EVENT_PREFIX,
		doc.name,
		now_datetime().strftime("%Y%m%d%H%M%S%f"),
	)
	subject = _("Reminder: {0}'s Trial Class on {1}").format(context["student_name"], context["date_display"])
	message = _trial_class_reminder_email_message(context)
	log_name = _create_notification_log(
		event_key=event_key,
		recipient=context["recipient"],
		subject=subject,
		message=message,
		document_type="Inquiry",
		document_name=doc.name,
	)

	try:
		mail_result = sendmail_or_skip(
			action="trial_class_reminder",
			recipients=[context["recipient"]["email"]],
			subject=subject,
			message=message,
			reference_doctype="Inquiry",
			reference_name=doc.name,
			reply_to=context["school_email"],
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(log_name, reason)
			_set_inquiry_reminder_status(doc.name, "Failed")
			return {
				"sent": False,
				"skipped": True,
				"recipient": context["recipient"]["email"],
				"reason": reason,
				"notification_log": log_name,
			}

		_mark_notification_sent(log_name)
		_set_inquiry_reminder_status(doc.name, "Sent")
		return {
			"sent": True,
			"recipient": context["recipient"]["email"],
			"notification_log": log_name,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS trial class reminder failed: {0}".format(doc.name))
		_mark_notification_failed(log_name, "Email send failed.")
		_set_inquiry_reminder_status(doc.name, "Failed")
		return {
			"sent": False,
			"recipient": context["recipient"]["email"],
			"reason": "Email send failed.",
			"notification_log": log_name,
		}


def get_trial_class_reminder_summary(inquiry: str):
	if not inquiry or not _notification_log_available():
		return None

	meta = frappe.get_meta("Notification Log")
	fields = _safe_notification_fields()
	filters = {"document_type": "Inquiry", "document_name": inquiry}
	if meta.has_field("event_key"):
		filters["event_key"] = ["like", "{0}%".format(TRIAL_CLASS_REMINDER_EVENT_PREFIX)]
	else:
		filters["subject"] = ["like", "Reminder:%Trial Class%"]

	rows = frappe.get_all(
		"Notification Log",
		filters=filters,
		fields=fields,
		order_by="creation desc",
		limit_page_length=1,
	)
	return _notification_payload(rows[0]) if rows else None


def enqueue_session_staff_notification(
	event: str,
	*,
	course_session: str,
	previous_course_session: str | None = None,
	student: str,
	source_doctype: str,
	source_document: str,
):
	"""Queue one idempotent operational email after the source transaction commits."""
	if not session_staff_notification_enabled(event):
		return {
			"queued": False,
			"skipped": True,
			"reason": TRIAL_ADDED_NOTIFICATION_DISABLED_REASON,
		}

	try:
		context = _session_staff_notification_context(
			event,
			course_session,
			student,
			previous_course_session=previous_course_session,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS session staff notification context failed: {0}".format(source_document))
		return {"queued": False, "reason": "Notification context could not be prepared."}

	event_key = _session_staff_notification_event_key(
		event,
		course_session,
		student,
		source_document,
		previous_course_session=previous_course_session,
	)
	subject = _session_staff_notification_subject(context)
	message = _session_staff_notification_email_message(context)
	if _session_staff_notification_already_logged(event_key, source_doctype, source_document, subject):
		return {"queued": False, "skipped": True, "reason": "Notification already recorded."}

	recipient = {"email": ", ".join(context["recipients"])}
	log_name = _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type=source_doctype,
		document_name=source_document,
	)
	if not context["recipients"]:
		reason = "No recipient email found for this session notification."
		_mark_notification_failed(log_name, reason)
		return {"queued": False, "skipped": True, "reason": reason, "notification_log": log_name}

	_mark_notification_queued(log_name)
	if not outbound_email_enabled():
		reason = email_block_reason()
		_mark_notification_failed(log_name, reason)
		return {
			"queued": False,
			"skipped": True,
			"reason": reason,
			"notification_log": log_name,
			"recipients": context["recipients"],
		}

	frappe.enqueue(
		"qas_custom.modules.notifications.commands.send_session_staff_notification_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		# `event` is reserved by frappe.enqueue for queue metadata, so use a
		# distinct keyword for the notification's business event.
		notification_event=event,
		course_session=course_session,
		previous_course_session=previous_course_session,
		student=student,
		source_doctype=source_doctype,
		source_document=source_document,
		notification_log=log_name,
	)
	return {
		"queued": True,
		"notification_log": log_name,
		"recipients": context["recipients"],
		"missing_recipients": context["missing_recipients"],
	}


def send_session_staff_notification_job(
	notification_event: str,
	*,
	course_session: str,
	previous_course_session: str | None = None,
	student: str,
	source_doctype: str,
	source_document: str,
	notification_log: str | None = None,
):
	if not session_staff_notification_enabled(notification_event):
		_mark_notification_failed(notification_log, TRIAL_ADDED_NOTIFICATION_DISABLED_REASON)
		return {
			"sent": False,
			"skipped": True,
			"reason": TRIAL_ADDED_NOTIFICATION_DISABLED_REASON,
			"notification_log": notification_log,
		}
	if not _session_staff_notification_is_current(
		notification_event,
		course_session,
		student,
		source_document,
		previous_course_session=previous_course_session,
	):
		reason = "The source event is no longer eligible for notification."
		_mark_notification_failed(notification_log, reason)
		return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}
	return send_session_staff_notification(
		notification_event,
		course_session=course_session,
		previous_course_session=previous_course_session,
		student=student,
		source_doctype=source_doctype,
		source_document=source_document,
		notification_log=notification_log,
	)


def send_session_staff_notification(
	event: str,
	*,
	course_session: str,
	previous_course_session: str | None = None,
	student: str,
	source_doctype: str,
	source_document: str,
	notification_log: str | None = None,
):
	if not session_staff_notification_enabled(event):
		_mark_notification_failed(notification_log, TRIAL_ADDED_NOTIFICATION_DISABLED_REASON)
		return {
			"sent": False,
			"skipped": True,
			"reason": TRIAL_ADDED_NOTIFICATION_DISABLED_REASON,
			"notification_log": notification_log,
		}

	try:
		context = _session_staff_notification_context(
			event,
			course_session,
			student,
			previous_course_session=previous_course_session,
		)
		subject = _session_staff_notification_subject(context)
		message = _session_staff_notification_email_message(context)
		if not context["recipients"]:
			reason = "No recipient email found for this session notification."
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}

		kwargs = {
			"action": "session_staff_{0}".format(event),
			"recipients": context["recipients"],
			"subject": subject,
			"message": message,
			"reference_doctype": source_doctype,
			"reference_name": source_document,
			"delayed": False,
		}
		if context.get("school_email"):
			kwargs["reply_to"] = context["school_email"]
		mail_result = sendmail_or_skip(**kwargs)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}

		_mark_notification_sent(notification_log)
		return {
			"sent": True,
			"notification_log": notification_log,
			"recipients": context["recipients"],
			"missing_recipients": context["missing_recipients"],
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS session staff notification failed: {0}".format(source_document))
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed.", "notification_log": notification_log}


def session_staff_notification_enabled(event: str) -> bool:
	"""Allow trial teacher mail to be paused without affecting other operational mail."""
	if event not in TRIAL_NOTIFICATION_EVENTS:
		return True
	configured = frappe.conf.get(TRIAL_ADDED_NOTIFICATION_CONFIG)
	if configured is None:
		return True
	return cint(configured) == 1


def _session_staff_notification_context(
	event: str,
	course_session: str,
	student: str,
	*,
	previous_course_session: str | None = None,
):
	if event not in SESSION_STAFF_NOTIFICATION_EVENTS:
		frappe.throw(_("Unsupported session staff notification event."))
	context = _session_staff_course_context(course_session, student)
	context["event"] = event
	if event in TRIAL_NOTIFICATION_EVENTS:
		context["recipients"] = list(dict.fromkeys(context["teacher_recipients"] + context.get("campus_admin_recipients", [])))
		context["missing_recipients"] = [] if context["teacher_recipients"] else ["teacher email"]
	else:
		context["recipients"] = list(context["teacher_recipients"])
		if context["school_email"]:
			context["recipients"].append(context["school_email"])
		context["recipients"] = list(dict.fromkeys(context["recipients"]))
		context["missing_recipients"] = []
		if not context["teacher_recipients"]:
			context["missing_recipients"].append("teacher email")
		if not context["school_email"]:
			context["missing_recipients"].append("school email")
	if event == "trial_rescheduled":
		if not previous_course_session:
			frappe.throw(_("Previous course session is required for a trial reschedule notification."))
		previous = _session_staff_course_context(previous_course_session, student)
		context["previous"] = previous
		context["recipients"] = list(dict.fromkeys(
			previous["teacher_recipients"]
			+ previous.get("campus_admin_recipients", [])
			+ context["teacher_recipients"]
			+ context.get("campus_admin_recipients", [])
		))
		context["missing_recipients"] = []
		if not previous["teacher_recipients"]:
			context["missing_recipients"].append("original session teacher email")
		if not context["teacher_recipients"]:
			context["missing_recipients"].append("new session teacher email")
	return context


def _session_staff_course_context(course_session: str, student: str):
	from qas_custom.services.campus_admin_accounts import get_active_campus_admin_emails

	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "teacher_override"],
		as_dict=True,
	)
	if not session or not session.get("weekly_timeslot"):
		frappe.throw(_("Course session was not found or is missing a weekly timeslot."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.weekly_timeslot,
		["name", "course", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))

	teacher_name = session.get("teacher_override") or timeslot.get("teacher")
	teacher = frappe.db.get_value(
		"Teacher",
		teacher_name,
		["name", "teacher_name", "email", "user"],
		as_dict=True,
	) if teacher_name else None
	teacher_email = _session_staff_teacher_email(teacher)
	student_row = frappe.db.get_value("Student", student, ["name", "student_name"], as_dict=True) or {}
	settings = get_invoice_settings()
	school_email = (settings.get("school_email") or "").strip().lower()
	teacher_recipients = [teacher_email] if teacher_email else []
	campus_admin_recipients = get_active_campus_admin_emails(timeslot.get("campus"))

	return {
		"course_session": course_session,
		"student": student,
		"student_name": student_row.get("student_name") or student,
		"course": timeslot.get("course") or "Class",
		"campus": timeslot.get("campus") or "Not assigned",
		"classroom": timeslot.get("classroom") or "Not assigned",
		"date_display": formatdate(session.get("session_date"), "d MMMM yyyy") if session.get("session_date") else "Not assigned",
		"day_of_week": timeslot.get("day_of_week") or "",
		"start_time": _session_staff_time(timeslot.get("start_time")),
		"end_time": _session_staff_time(timeslot.get("end_time")),
		"teacher_name": (teacher or {}).get("teacher_name") or teacher_name or "Unassigned",
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_email": school_email,
		"teacher_recipients": teacher_recipients,
		"campus_admin_recipients": campus_admin_recipients,
	}


def _session_staff_teacher_email(teacher):
	if not teacher:
		return ""
	for fieldname in ["email", "email_id", "contact_email"]:
		value = teacher.get(fieldname)
		if value:
			return str(value).strip().lower()
	user = teacher.get("user")
	if user and frappe.db.exists("User", user):
		return (frappe.db.get_value("User", user, "email") or user or "").strip().lower()
	return ""


def _session_staff_notification_event_key(
	event: str,
	course_session: str,
	student: str,
	source_document: str,
	*,
	previous_course_session: str | None = None,
):
	event_labels = {
		"leave_requested": "leave",
		"makeup_booked": "makeup",
		"trial_added": "trial",
		"trial_cancelled": "trial-cancel",
		"trial_rescheduled": "trial-reschedule",
	}
	event_label = event_labels.get(event, event)
	identity = "\x1f".join((event, source_document or "", previous_course_session or "", course_session or "", student or ""))
	digest = sha256(identity.encode()).hexdigest()[:24]
	return "{0}{1}:{2}".format(SESSION_STAFF_NOTIFICATION_EVENT_PREFIX, event_label, digest)


def _session_staff_notification_already_logged(event_key, document_type, document_name, subject):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	if meta.has_field("event_key"):
		return bool(frappe.db.exists("Notification Log", {"event_key": event_key}))
	return bool(frappe.db.exists("Notification Log", {"document_type": document_type, "document_name": document_name, "subject": subject}))


def _session_staff_notification_is_current(
	event,
	course_session,
	student,
	source_document,
	*,
	previous_course_session=None,
):
	if event == "leave_requested":
		row = frappe.db.get_value("Leave Request", source_document, ["status", "course_session", "student"], as_dict=True)
		return bool(row and row.get("status") == "Approved" and row.get("course_session") == course_session and row.get("student") == student)
	if event == "makeup_booked":
		row = frappe.db.get_value("Makeup Voucher", source_document, ["status", "used_on_session", "student", "used_by_student"], as_dict=True)
		return bool(
			row
			and row.get("status") == "Used"
			and row.get("used_on_session") == course_session
			and (row.get("used_by_student") or row.get("student")) == student
		)
	if event in TRIAL_NOTIFICATION_EVENTS:
		row = _get_trial_notification_inquiry(source_document)
		if event == "trial_cancelled":
			return bool(
				row
				and row.get("inquiry_type") == "Trial Lesson"
				and row.get("student") == student
				and (
					(row.get("status") == "Cancelled" and row.get("course_session") == course_session)
					or (row.get("status") != "Cancelled" and row.get("course_session") != course_session)
				)
			)
		return bool(
			row
			and row.get("inquiry_type") == "Trial Lesson"
			and row.get("status") != "Cancelled"
			and row.get("course_session") == course_session
			and row.get("student") == student
		)
	return False


def _get_trial_notification_inquiry(inquiry):
	return frappe.db.get_value("Inquiry", inquiry, ["inquiry_type", "status", "course_session", "student"], as_dict=True)


def _session_staff_notification_subject(context):
	prefixes = {
		"leave_requested": "Leave request",
		"makeup_booked": "Makeup class booked",
		"trial_added": "Trial student added",
		"trial_cancelled": "Trial student cancelled",
		"trial_rescheduled": "Trial student rescheduled",
	}
	return _("{0}: {1} - {2}").format(prefixes[context["event"]], context["student_name"], context["date_display"])


def _session_staff_notification_email_message(context):
	def value(key):
		return escape_html(str(context.get(key) or ""))

	intro = {
		"leave_requested": "A parent has requested leave for this student.",
		"makeup_booked": "This student has been booked into your session as a makeup class.",
		"trial_added": "This session now has a trial student.",
		"trial_cancelled": "This trial student will no longer attend the session.",
		"trial_rescheduled": "This trial student has been moved to a different session.",
	}[context["event"]]
	if context["event"] == "trial_rescheduled":
		return _trial_rescheduled_notification_email_message(context, intro)
	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">{title}</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						<table style="width:100%;border-collapse:collapse;">
							<tr><td style="padding:9px 0;color:#64748b;">Student</td><td style="padding:9px 0;text-align:right;font-weight:700;">{student_name}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Course</td><td style="padding:9px 0;text-align:right;font-weight:700;">{course}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Campus</td><td style="padding:9px 0;text-align:right;font-weight:700;">{campus}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Classroom</td><td style="padding:9px 0;text-align:right;font-weight:700;">{classroom}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Date</td><td style="padding:9px 0;text-align:right;font-weight:700;">{day_of_week} {date_display}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Time</td><td style="padding:9px 0;text-align:right;font-weight:700;">{start_time} - {end_time}</td></tr>
						</table>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=value("school_name"),
		title=escape_html(_session_staff_notification_subject(context)),
		intro=escape_html(intro),
		student_name=value("student_name"),
		course=value("course"),
		campus=value("campus"),
		classroom=value("classroom"),
		day_of_week=value("day_of_week"),
		date_display=value("date_display"),
		start_time=value("start_time"),
		end_time=value("end_time"),
	)


def _trial_rescheduled_notification_email_message(context, intro):
	def value(source, key):
		return escape_html(str(source.get(key) or ""))

	previous = context["previous"]
	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">{title}</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						<p style="font-weight:700;">Student: {student_name}</p>
						<table style="width:100%;border-collapse:collapse;">
							<tr><th style="padding:9px;text-align:left;">Session</th><th style="padding:9px;text-align:left;">Original</th><th style="padding:9px;text-align:left;">New</th></tr>
							<tr><td style="padding:9px;">Course</td><td style="padding:9px;">{old_course}</td><td style="padding:9px;">{new_course}</td></tr>
							<tr><td style="padding:9px;">Campus</td><td style="padding:9px;">{old_campus}</td><td style="padding:9px;">{new_campus}</td></tr>
							<tr><td style="padding:9px;">Classroom</td><td style="padding:9px;">{old_classroom}</td><td style="padding:9px;">{new_classroom}</td></tr>
							<tr><td style="padding:9px;">Date</td><td style="padding:9px;">{old_day} {old_date}</td><td style="padding:9px;">{new_day} {new_date}</td></tr>
							<tr><td style="padding:9px;">Time</td><td style="padding:9px;">{old_start} - {old_end}</td><td style="padding:9px;">{new_start} - {new_end}</td></tr>
						</table>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=value(context, "school_name"),
		title=escape_html(_session_staff_notification_subject(context)),
		intro=escape_html(intro),
		student_name=value(context, "student_name"),
		old_course=value(previous, "course"),
		new_course=value(context, "course"),
		old_campus=value(previous, "campus"),
		new_campus=value(context, "campus"),
		old_classroom=value(previous, "classroom"),
		new_classroom=value(context, "classroom"),
		old_day=value(previous, "day_of_week"),
		new_day=value(context, "day_of_week"),
		old_date=value(previous, "date_display"),
		new_date=value(context, "date_display"),
		old_start=value(previous, "start_time"),
		new_start=value(context, "start_time"),
		old_end=value(previous, "end_time"),
		new_end=value(context, "end_time"),
	)


def _session_staff_time(value):
	text = str(value or "").strip()
	return text[:5] if len(text) >= 5 else text


def _trial_class_reminder_context(inquiry_doc):
	session = None
	timeslot = None
	if inquiry_doc.get("course_session"):
		session = frappe.db.get_value(
			"Course Sessions",
			inquiry_doc.course_session,
			["name", "weekly_timeslot", "session_date"],
			as_dict=True,
		)
		if session and session.get("weekly_timeslot"):
			timeslot = frappe.db.get_value(
				"Weekly Timeslot",
				session.weekly_timeslot,
				["name", "course", "campus", "start_time", "end_time"],
				as_dict=True,
			)

	campus_name = (timeslot or {}).get("campus") or inquiry_doc.get("campus")
	campus = frappe.db.get_value("Campus", campus_name, ["name", "address"], as_dict=True) if campus_name else None
	settings = get_invoice_settings()
	student_name = _trial_class_reminder_student_name(inquiry_doc)
	parent_name = inquiry_doc.get("contact_name") or _trial_class_reminder_parent_name(inquiry_doc.get("parent"))
	date_value = (session or {}).get("session_date") or inquiry_doc.get("current_appointment_date")
	start_time = (timeslot or {}).get("start_time") or inquiry_doc.get("current_appointment_time")
	end_time = (timeslot or {}).get("end_time")
	context = {
		"recipient": _trial_class_reminder_recipient(inquiry_doc),
		"parent_name": parent_name or "Parent",
		"student_name": student_name,
		"course": (timeslot or {}).get("course") or inquiry_doc.get("preferred_course"),
		"campus": campus_name,
		"campus_address": (campus or {}).get("address"),
		"date_display": formatdate(date_value, "d MMMM yyyy") if date_value else None,
		"start_time": _trial_class_reminder_time(start_time),
		"end_time": _trial_class_reminder_time(end_time),
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_email": settings.get("school_email"),
		"school_phone": settings.get("school_phone"),
	}

	labels = {
		"recipient": "parent email",
		"student_name": "student name",
		"course": "course",
		"campus": "campus",
		"campus_address": "campus address",
		"date_display": "appointment date",
		"start_time": "appointment start time",
		"end_time": "appointment end time",
		"school_email": "school reply email",
		"school_phone": "school phone",
	}
	missing = [labels[key] for key in labels if not context.get(key) or (key == "recipient" and not context[key].get("email"))]
	if missing:
		frappe.throw(_("Cannot send trial class reminder. Missing: {0}.").format(", ".join(missing)))
	return context


def _trial_class_reminder_recipient(inquiry_doc):
	email = (inquiry_doc.get("contact_email") or "").strip()
	parent = inquiry_doc.get("parent")
	linked_user = None
	if parent:
		fields = ["name"]
		for fieldname in ["linked_user", "email", "email_id", "contact_email"]:
			if frappe.db.has_column("Parent", fieldname):
				fields.append(fieldname)
		parent_row = frappe.db.get_value("Parent", parent, fields, as_dict=True) or {}
		linked_user = parent_row.get("linked_user")
		if not email:
			email = _first_value(parent_row, ["email", "email_id", "contact_email"])
	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user
	return {"email": email, "for_user": linked_user, "parent": parent}


def _trial_class_reminder_parent_name(parent):
	if not parent:
		return None
	return frappe.db.get_value("Parent", parent, "parent_name")


def _trial_class_reminder_student_name(inquiry_doc):
	if inquiry_doc.get("student"):
		name = frappe.db.get_value("Student", inquiry_doc.student, "student_name")
		if name:
			return name
	return inquiry_doc.get("submitted_student_name")


def _trial_class_reminder_time(value):
	text = str(value or "").strip()
	return text[:5] if len(text) >= 5 else text


def _trial_class_reminder_email_message(context, heading=None, intro=None):
	def value(key):
		return escape_html(context.get(key) or "")

	heading = heading or _("Trial class reminder")
	intro = intro or _("This is a friendly reminder about your child's upcoming trial class.")

	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">{heading}</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Hello {parent_name},</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						<table style="width:100%;border-collapse:collapse;margin:0 0 22px;">
							<tr><td style="padding:9px 0;color:#64748b;">Student</td><td style="padding:9px 0;text-align:right;font-weight:700;">{student_name}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Course</td><td style="padding:9px 0;text-align:right;font-weight:700;">{course}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Campus</td><td style="padding:9px 0;text-align:right;font-weight:700;">{campus}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Address</td><td style="padding:9px 0;text-align:right;font-weight:700;">{campus_address}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Date</td><td style="padding:9px 0;text-align:right;font-weight:700;">{date_display}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Time</td><td style="padding:9px 0;text-align:right;font-weight:700;">{start_time} - {end_time}</td></tr>
						</table>
						<p style="margin:0;font-size:15px;line-height:1.5;color:#475569;">If you are unable to attend or need to make changes, please reply to this email or call {school_name} on {school_phone}.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=value("school_name"),
		heading=escape_html(heading),
		intro=escape_html(intro),
		parent_name=value("parent_name"),
		student_name=value("student_name"),
		course=value("course"),
		campus=value("campus"),
		campus_address=value("campus_address").replace("\n", "<br>"),
		date_display=value("date_display"),
		start_time=value("start_time"),
		end_time=value("end_time"),
		school_phone=value("school_phone"),
	)


def _set_inquiry_reminder_status(inquiry, status):
	if frappe.db.has_column("Inquiry", "reminder_status"):
		frappe.db.set_value("Inquiry", inquiry, "reminder_status", status, update_modified=False)


def _invoice_recipient(invoice_doc):
	parent = invoice_doc.get("parent") or _parent_for_customer(invoice_doc.customer)
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

	if not email and invoice_doc.customer:
		email = _customer_contact_email(invoice_doc.customer)

	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user

	return {"email": email, "for_user": linked_user, "parent": parent, "customer": invoice_doc.customer}


def _parent_for_customer(customer):
	if not customer:
		return None
	if frappe.db.exists("DocType", "Parent") and frappe.db.has_column("Parent", "customer"):
		return frappe.db.get_value("Parent", {"customer": customer}, "name")
	return None


def _customer_contact_email(customer):
	if not customer or not frappe.db.exists("DocType", "Dynamic Link") or not frappe.db.exists("DocType", "Contact"):
		return None

	contacts = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		pluck="parent",
		limit_page_length=20,
	)
	contacts = [contact for contact in contacts if contact]
	if not contacts:
		return None

	fields = ["name"]
	for fieldname in ["email_id", "email", "contact_email", "is_primary_contact"]:
		if frappe.db.has_column("Contact", fieldname):
			fields.append(fieldname)
	filters = {"name": ["in", contacts]}
	if frappe.db.has_column("Contact", "disabled"):
		filters["disabled"] = 0
	order_by = "is_primary_contact desc, modified desc" if frappe.db.has_column("Contact", "is_primary_contact") else "modified desc"
	for row in frappe.get_all("Contact", filters=filters, fields=fields, order_by=order_by, limit_page_length=20):
		email = _first_value(row, ["email_id", "email", "contact_email"])
		if email:
			return email

	if not frappe.db.exists("DocType", "Contact Email") or not frappe.db.has_column("Contact Email", "email_id"):
		return None
	email_fields = ["parent", "email_id"]
	if frappe.db.has_column("Contact Email", "is_primary"):
		email_fields.append("is_primary")
	order_by = "is_primary desc, idx asc" if frappe.db.has_column("Contact Email", "is_primary") else "idx asc"
	for row in frappe.get_all(
		"Contact Email",
		filters={"parent": ["in", contacts]},
		fields=email_fields,
		order_by=order_by,
		limit_page_length=20,
	):
		if row.get("email_id"):
			return row.get("email_id")
	return None


def _invoice_notification_event_key(invoice_doc, event):
	base = f"invoice_{event}:{invoice_doc.name}"
	if event == "resent":
		return f"{base}:{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	return base


def _invoice_email_subject(invoice_doc, event):
	action = _("Invoice ready") if event == "approved" else _("Invoice resent")
	return _("Queensland Art School - {0} {1}").format(action, invoice_doc.name)


def render_parent_invoice_pdf(invoice: str, *, store_credit_applied=None, payable_amount=None):
	doc = frappe.get_doc("Sales Invoice", invoice)
	amounts = _invoice_notification_amounts(doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	context = build_parent_invoice_context(
		doc,
		store_credit_applied=amounts["store_credit_applied"],
		payable_amount=amounts["payable_amount"],
		invoice_link=parent_portal_invoice_link(doc.name),
		include_portal_link=_invoice_portal_links_enabled(),
	)
	html = _invoice_pdf_html(context)
	return get_pdf(html)


def _invoice_pdf_attachment(invoice: str, *, store_credit_applied=None, payable_amount=None):
	pdf_content = render_parent_invoice_pdf(
		invoice,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
	)
	file_doc = save_file(
		f"{invoice}.pdf",
		pdf_content,
		"Sales Invoice",
		invoice,
		is_private=1,
	)
	return {
		"fid": file_doc.name,
	}


def _invoice_notification_amounts(invoice_doc, *, store_credit_applied=None, payable_amount=None):
	return resolve_invoice_print_amounts(
		invoice_doc,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
	)


def _invoice_pdf_html(context):
	rows = "\n".join(_invoice_pdf_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="4" class="muted">Invoice details are included in this PDF.</td></tr>"""

	payment_block = _invoice_pdf_payment_block(context)
	invoice_message = _invoice_pdf_message(context.get("invoice_message"))
	return """
<!doctype html>
<html>
<head>
	<meta charset="utf-8">
	<style>
		@page {{ size: A4; margin: 20mm 18mm; }}
		body {{ color: #172033; font-family: Arial, sans-serif; font-size: 12px; line-height: 1.45; }}
		* {{ box-sizing: border-box; }}
		.header {{ border-bottom: 2px solid #172033; margin-bottom: 22px; padding-bottom: 18px; width: 100%; }}
		.brand {{ color: #e85f47; font-size: 12px; font-weight: 700; letter-spacing: .04em; margin: 0 0 8px; text-transform: uppercase; }}
		h1 {{ font-size: 28px; font-weight: 800; margin: 0; }}
		.muted {{ color: #64748b; }}
		.summary {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; margin: 22px 0; width: 100%; }}
		.summary td {{ padding: 14px 12px; width: 33.333%; }}
		.summary span {{ color: #64748b; display: block; font-size: 12px; margin-bottom: 4px; }}
		.summary strong {{ display: block; font-size: 18px; }}
		.payable {{ color: #e85f47; }}
		.note {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; margin: 0 0 22px; padding: 14px 16px; }}
		table.items {{ border-collapse: collapse; margin-top: 18px; width: 100%; }}
		table.items th {{ background: #f1f5f9; color: #64748b; font-size: 11px; padding: 10px 8px; text-align: left; text-transform: uppercase; }}
		table.items td {{ border-bottom: 1px solid #e5e7eb; padding: 11px 8px; vertical-align: top; }}
		.totals {{ margin-left: auto; margin-top: 22px; width: 360px; }}
		.totals td {{ padding: 7px 0; }}
		.totals .final td {{ border-top: 2px solid #172033; font-size: 16px; font-weight: 800; padding-top: 12px; }}
		.payment {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 10px; margin-top: 26px; padding: 14px 16px; }}
		.payment strong {{ display: block; font-size: 14px; margin-bottom: 4px; }}
		.right {{ text-align: right; }}
	</style>
</head>
<body>
	<table class="header">
		<tr>
			<td>
				<p class="brand">{school_name}</p>
				<h1>Invoice</h1>
				<div class="muted">{invoice}</div>
				{school_identity}
			</td>
			<td class="right">
				<strong>Due date</strong><br>{due_date}<br><br>
				<strong>Invoice date</strong><br>{posting_date}
			</td>
		</tr>
	</table>

	<table class="summary">
		<tr>
			<td><span>Invoice total</span><strong>AUD ${total:.2f}</strong></td>
			<td><span>Store credit applied</span><strong>AUD ${credit:.2f}</strong></td>
			<td><span>Amount payable</span><strong class="payable">AUD ${payable:.2f}</strong></td>
		</tr>
	</table>

	{invoice_message}

	<table class="items">
		<thead>
			<tr>
				<th>Student</th>
				<th>Description</th>
				<th class="right">Unit price</th>
				<th class="right">Amount</th>
			</tr>
		</thead>
		<tbody>{rows}</tbody>
	</table>

	<table class="totals">
		<tr><td>Invoice total</td><td class="right"><strong>AUD ${total:.2f}</strong></td></tr>
		<tr><td>Store credit applied</td><td class="right"><strong>AUD ${credit:.2f}</strong></td></tr>
		<tr class="final"><td>Amount payable</td><td class="right">AUD ${payable:.2f}</td></tr>
	</table>

	{payment_block}
</body>
</html>
	""".format(
		invoice=escape_html(context["invoice"]),
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		school_identity=_school_identity_pdf_html(context),
		due_date=escape_html(context["due_date"] or "-"),
		posting_date=escape_html(context["posting_date"] or "-"),
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		payable=flt(context["payable_amount"]),
		invoice_message=invoice_message,
		rows=rows,
		payment_block=payment_block,
	)


def _invoice_pdf_item_row(item):
	return """
		<tr>
			<td><strong>{student}</strong></td>
			<td>{description}</td>
			<td class="right">AUD ${rate:.2f}</td>
			<td class="right"><strong>AUD ${amount:.2f}</strong></td>
		</tr>
	""".format(
		student=escape_html(item.get("student") or ""),
		description=escape_html(item.get("description") or ""),
		rate=flt(item.get("rate")),
		amount=flt(item.get("amount")),
	)


def _invoice_pdf_message(value):
	if not value:
		return ""
	return """<div class="note">{0}</div>""".format(escape_html(value).replace("\n", "<br>"))


def _invoice_pdf_payment_block(context):
	if flt(context["payable_amount"]) <= 0:
		message = (
			"This invoice is fully covered by store credit. No payment is required."
			if flt(context["store_credit_applied"]) > 0
			else "This invoice has no amount payable. No payment is required."
		)
		return """<div class="payment"><strong>Payment</strong>{0}</div>""".format(escape_html(message))

	rows = []
	for label, fieldname in [
		("Account name", "bank_account_name"),
		("BSB", "bank_bsb"),
		("Account number", "bank_account_number"),
	]:
		value = context.get(fieldname)
		if value:
			rows.append(
				"""<tr><td>{0}</td><td class="right"><strong>{1}</strong></td></tr>""".format(
					escape_html(label),
					escape_html(value),
				)
			)
	reference_note = escape_html(context.get("bank_reference_note") or "")
	return """
		<div class="payment">
			<strong>Payment</strong>
			Please arrange payment by {methods}. If you have already paid, no further action is needed.
			<table style="margin-top:12px;width:100%;">{rows}</table>
			<div style="margin-top:10px;">{reference_note}</div>
		</div>
	""".format(
		methods=escape_html(context.get("accepted_payment_methods") or "Bank transfer, cash, or POS"),
		rows="".join(rows),
		reference_note=reference_note,
	)


def _invoice_email_message(invoice_doc, event, store_credit_applied, payable_amount, payment_link):
	context = build_parent_invoice_context(
		invoice_doc,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
		include_portal_link=_invoice_portal_links_enabled(),
	)
	greeting = _invoice_email_greeting(context)
	intro = (
		_("Your invoice is attached to this email.")
		if event == "approved"
		else _("We have resent this invoice for your reference.")
	)
	if flt(context["payable_amount"]) > 0:
		payment_line = _("Please arrange payment by {0}.").format(context["accepted_payment_methods"] or _("bank transfer, cash, or POS"))
	elif flt(context["store_credit_applied"]) > 0:
		payment_line = _("No payment is required because this invoice is fully covered by store credit.")
	else:
		payment_line = _("No payment is required for this invoice.")
	invoice_message = _html_multiline(context.get("invoice_message"))
	bank_details = _invoice_email_bank_details(context) if flt(context["payable_amount"]) > 0 else ""
	rows = "\n".join(_invoice_email_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="3" style="padding:12px;color:#64748b;">Invoice details are included in the attached PDF.</td></tr>"""

	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Invoice {invoice}</h1>
						{school_identity}
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						{invoice_message}

						<table style="width:100%;border-collapse:collapse;margin:0 0 18px;">
							<tr>
								<td style="padding:10px 0;color:#64748b;">Due date</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">{due_date}</td>
							</tr>
							<tr>
								<td style="padding:10px 0;color:#64748b;">Invoice total</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${total:.2f}</td>
							</tr>
							<tr>
								<td style="padding:10px 0;color:#64748b;">Store credit applied</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${credit:.2f}</td>
							</tr>
							<tr>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;font-size:17px;font-weight:700;">Amount payable</td>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;text-align:right;font-size:20px;font-weight:800;color:#e85f47;">AUD ${payable:.2f}</td>
							</tr>
						</table>

						<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;margin:0 0 22px;">
							<thead>
								<tr style="background:#f1f5f9;">
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Student</th>
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Description</th>
									<th align="right" style="padding:10px;font-size:12px;color:#64748b;">Amount</th>
								</tr>
							</thead>
							<tbody>{rows}</tbody>
						</table>

						<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;">{payment_line}</p>
						{bank_details}
						{portal_action}
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If you have already paid, no further action is needed.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		invoice=context["invoice"],
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		school_identity=_school_identity_email_html(context),
		greeting=greeting,
		intro=intro,
		invoice_message=invoice_message,
		due_date=context["due_date"] or "-",
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		payable=flt(context["payable_amount"]),
		rows=rows,
		payment_line=payment_line,
		bank_details=bank_details,
		portal_action=_invoice_email_portal_action(context),
	)


def _invoice_cancellation_email_message(invoice_doc, reason=None):
	context = build_parent_invoice_context(invoice_doc, include_portal_link=False)
	reason_html = ""
	if reason:
		reason_html = """<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;"><strong>Reason:</strong> {0}</p>""".format(
			escape_html(reason)
		)
	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Invoice cancelled</h1>
						{school_identity}
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Invoice <strong>{invoice}</strong> has been cancelled.</p>
						{reason_html}
						<p style="margin:0;font-size:15px;line-height:1.5;color:#475569;">If you have any questions, please contact {school_name}.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		school_identity=_school_identity_email_html(context),
		greeting=_invoice_email_greeting(context),
		invoice=escape_html(invoice_doc.name),
		reason_html=reason_html,
	)


def _invoice_email_portal_action(context):
	invoice_link = context.get("invoice_link")
	if not invoice_link:
		return ""
	return '''<p style="margin:0 0 22px;">
		<a href="{0}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">View invoice</a>
	</p>'''.format(escape_html(invoice_link))


def _invoice_portal_links_enabled():
	return bool(cint(frappe.conf.get("qas_invoice_portal_links_enabled") or 0))


def _school_identity_email_html(context):
	parts = _school_identity_parts(context)
	if not parts:
		return ""
	return """<div style="margin-top:6px;color:#cbd5e1;font-size:13px;line-height:1.45;">{0}</div>""".format("<br>".join(parts))


def _school_identity_pdf_html(context):
	parts = _school_identity_parts(context)
	if not parts:
		return ""
	return """<div class="muted" style="margin-top:6px;">{0}</div>""".format("<br>".join(parts))


def _school_identity_parts(context):
	parts = []
	legal_name = context.get("legal_name")
	abn = context.get("abn")
	if legal_name:
		parts.append(escape_html(legal_name))
	if abn:
		abn_text = str(abn).strip()
		if abn_text.upper().startswith("ABN"):
			parts.append(escape_html(abn_text))
		else:
			parts.append(escape_html(_("ABN {0}").format(abn_text)))
	return parts


def _invoice_email_greeting(context):
	recipient_name = context.get("recipient_name")
	if recipient_name:
		return _("Hi {0},").format(escape_html(recipient_name))
	return _("Hi,")


def _html_multiline(value):
	if not value:
		return ""
	return """<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;">{0}</p>""".format(
		escape_html(value).replace("\n", "<br>")
	)


def _invoice_email_bank_details(context):
	rows = []
	for label, fieldname in [
		(_("Account name"), "bank_account_name"),
		(_("BSB"), "bank_bsb"),
		(_("Account number"), "bank_account_number"),
	]:
		value = context.get(fieldname)
		if value:
			rows.append(
				"""<tr><td style="padding:6px 0;color:#64748b;">{0}</td><td style="padding:6px 0;text-align:right;font-weight:700;">{1}</td></tr>""".format(
					escape_html(label),
					escape_html(value),
				)
			)
	if not rows and not context.get("bank_reference_note"):
		return ""

	reference_note = _html_multiline(context.get("bank_reference_note"))
	return """
		<div style="margin:0 0 20px;padding:14px;border:1px solid #fed7aa;border-radius:10px;background:#fff7ed;">
			<p style="margin:0 0 8px;font-weight:700;color:#7c2d12;">Bank transfer details</p>
			<table style="width:100%;border-collapse:collapse;">{rows}</table>
			{reference_note}
		</div>
	""".format(rows="".join(rows), reference_note=reference_note)


def _invoice_email_item_row(item):
	return """
		<tr>
			<td style="padding:10px;border-top:1px solid #e5e7eb;font-weight:700;">{student}</td>
			<td style="padding:10px;border-top:1px solid #e5e7eb;">{description}</td>
			<td style="padding:10px;border-top:1px solid #e5e7eb;text-align:right;font-weight:700;">AUD ${amount:.2f}</td>
		</tr>
	""".format(
		student=escape_html(item.get("student") or ""),
		description=escape_html(item.get("description") or ""),
		amount=flt(item.get("amount")),
	)


def render_parent_receipt_pdf(invoice: str, *, payment_entry=None, amounts=None, payment_context=None):
	doc = frappe.get_doc("Sales Invoice", invoice)
	amounts = amounts or _invoice_notification_amounts(doc)
	payment_doc = _payment_entry_doc(payment_entry)
	payment_context = payment_context or _receipt_payment_context(doc, payment_doc, amounts)
	context = build_parent_invoice_context(
		doc,
		store_credit_applied=amounts["store_credit_applied"],
		payable_amount=0,
		invoice_link=parent_portal_invoice_link(doc.name),
		include_portal_link=_invoice_portal_links_enabled(),
	)
	context["receipt"] = payment_context
	html = _receipt_pdf_html(context)
	return get_pdf(html)


def _receipt_pdf_attachment(invoice: str, *, payment_entry=None, amounts=None, payment_context=None):
	pdf_content = render_parent_receipt_pdf(
		invoice,
		payment_entry=payment_entry,
		amounts=amounts,
		payment_context=payment_context,
	)
	file_doc = save_file(
		f"{invoice}-receipt.pdf",
		pdf_content,
		"Sales Invoice",
		invoice,
		is_private=1,
	)
	return {"fid": file_doc.name}


def _receipt_email_subject(invoice_doc):
	return _("Queensland Art School - Payment receipt {0}").format(invoice_doc.name)


def _receipt_email_message(invoice_doc, amounts, payment_context):
	context = build_parent_invoice_context(
		invoice_doc,
		store_credit_applied=amounts["store_credit_applied"],
		payable_amount=0,
		payment_link=parent_portal_invoice_link(invoice_doc.name),
		include_portal_link=_invoice_portal_links_enabled(),
	)
	greeting = _invoice_email_greeting(context)
	rows = "\n".join(_invoice_email_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="3" style="padding:12px;color:#64748b;">Invoice details are included in the attached receipt PDF.</td></tr>"""

	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Payment receipt</h1>
						<div style="margin-top:6px;color:#cbd5e1;">Invoice {invoice}</div>
						{school_identity}
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Thank you. This invoice is fully paid.</p>

						<table style="width:100%;border-collapse:collapse;margin:0 0 18px;">
							<tr><td style="padding:10px 0;color:#64748b;">Payment date</td><td style="padding:10px 0;text-align:right;font-weight:700;">{payment_date}</td></tr>
							<tr><td style="padding:10px 0;color:#64748b;">Payment method</td><td style="padding:10px 0;text-align:right;font-weight:700;">{payment_method}</td></tr>
							<tr><td style="padding:10px 0;color:#64748b;">Invoice total</td><td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${total:.2f}</td></tr>
							<tr><td style="padding:10px 0;color:#64748b;">Store credit applied</td><td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${credit:.2f}</td></tr>
							<tr><td style="padding:10px 0;color:#64748b;">Payment received</td><td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${cash:.2f}</td></tr>
							<tr><td style="padding:12px 0;border-top:1px solid #e5e7eb;font-size:17px;font-weight:700;">Amount remaining</td><td style="padding:12px 0;border-top:1px solid #e5e7eb;text-align:right;font-size:20px;font-weight:800;color:#166534;">AUD ${remaining:.2f}</td></tr>
						</table>

						<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;margin:0 0 22px;">
							<thead>
								<tr style="background:#f1f5f9;">
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Student</th>
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Description</th>
									<th align="right" style="padding:10px;font-size:12px;color:#64748b;">Amount</th>
								</tr>
							</thead>
							<tbody>{rows}</tbody>
						</table>

						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">Receipt reference: {receipt_reference}</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		invoice=escape_html(context["invoice"]),
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		school_identity=_school_identity_email_html(context),
		greeting=greeting,
		payment_date=escape_html(payment_context.get("payment_date_display") or "-"),
		payment_method=escape_html(payment_context.get("payment_method") or "Payment"),
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		cash=flt(payment_context.get("payment_amount")),
		remaining=flt(payment_context.get("remaining_amount")),
		rows=rows,
		receipt_reference=escape_html(payment_context.get("receipt_reference") or context["invoice"]),
	)


def _receipt_pdf_html(context):
	receipt = context["receipt"]
	rows = "\n".join(_invoice_pdf_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="4" class="muted">Invoice details are included in this receipt PDF.</td></tr>"""

	return """
<!doctype html>
<html>
<head>
	<meta charset="utf-8">
	<style>
		@page {{ size: A4; margin: 20mm 18mm; }}
		body {{ color: #172033; font-family: Arial, sans-serif; font-size: 12px; line-height: 1.45; }}
		* {{ box-sizing: border-box; }}
		.header {{ border-bottom: 2px solid #172033; margin-bottom: 22px; padding-bottom: 18px; width: 100%; }}
		.brand {{ color: #e85f47; font-size: 12px; font-weight: 700; letter-spacing: .04em; margin: 0 0 8px; text-transform: uppercase; }}
		h1 {{ font-size: 28px; font-weight: 800; margin: 0; }}
		.muted {{ color: #64748b; }}
		.summary {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; margin: 22px 0; width: 100%; }}
		.summary td {{ padding: 14px 12px; width: 25%; }}
		.summary span {{ color: #64748b; display: block; font-size: 12px; margin-bottom: 4px; }}
		.summary strong {{ display: block; font-size: 17px; }}
		table.items {{ border-collapse: collapse; margin-top: 18px; width: 100%; }}
		table.items th {{ background: #f1f5f9; color: #64748b; font-size: 11px; padding: 10px 8px; text-align: left; text-transform: uppercase; }}
		table.items td {{ border-bottom: 1px solid #e5e7eb; padding: 11px 8px; vertical-align: top; }}
		.totals {{ margin-left: auto; margin-top: 22px; width: 360px; }}
		.totals td {{ padding: 7px 0; }}
		.totals .final td {{ border-top: 2px solid #172033; font-size: 16px; font-weight: 800; padding-top: 12px; }}
		.receipt-note {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; margin-top: 26px; padding: 14px 16px; }}
		.right {{ text-align: right; }}
	</style>
</head>
<body>
	<table class="header">
		<tr>
			<td>
				<p class="brand">{school_name}</p>
				<h1>Payment receipt</h1>
				<div class="muted">Invoice {invoice}</div>
				{school_identity}
			</td>
			<td class="right">
				<strong>Payment date</strong><br>{payment_date}<br><br>
				<strong>Payment method</strong><br>{payment_method}
			</td>
		</tr>
	</table>

	<table class="summary">
		<tr>
			<td><span>Invoice total</span><strong>AUD ${total:.2f}</strong></td>
			<td><span>Store credit applied</span><strong>AUD ${credit:.2f}</strong></td>
			<td><span>Payment received</span><strong>AUD ${cash:.2f}</strong></td>
			<td><span>Amount remaining</span><strong>AUD ${remaining:.2f}</strong></td>
		</tr>
	</table>

	<table class="items">
		<thead>
			<tr>
				<th>Student</th>
				<th>Description</th>
				<th class="right">Unit price</th>
				<th class="right">Amount</th>
			</tr>
		</thead>
		<tbody>{rows}</tbody>
	</table>

	<table class="totals">
		<tr><td>Invoice total</td><td class="right"><strong>AUD ${total:.2f}</strong></td></tr>
		<tr><td>Store credit applied</td><td class="right"><strong>AUD ${credit:.2f}</strong></td></tr>
		<tr><td>Payment received</td><td class="right"><strong>AUD ${cash:.2f}</strong></td></tr>
		<tr class="final"><td>Amount remaining</td><td class="right">AUD ${remaining:.2f}</td></tr>
	</table>

	<div class="receipt-note">Receipt reference: {receipt_reference}</div>
</body>
</html>
	""".format(
		invoice=escape_html(context["invoice"]),
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		school_identity=_school_identity_pdf_html(context),
		payment_date=escape_html(receipt.get("payment_date_display") or "-"),
		payment_method=escape_html(receipt.get("payment_method") or "Payment"),
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		cash=flt(receipt.get("payment_amount")),
		remaining=flt(receipt.get("remaining_amount")),
		rows=rows,
		receipt_reference=escape_html(receipt.get("receipt_reference") or context["invoice"]),
	)


def _receipt_payment_context(invoice_doc, payment_doc, amounts, source=None):
	store_credit = flt(amounts.get("store_credit_applied"))
	payment_amount = _payment_entry_invoice_amount(payment_doc, invoice_doc.name) if payment_doc else _submitted_payment_amount(invoice_doc.name)
	remaining = min(_invoice_outstanding_amount(invoice_doc), flt(amounts.get("payable_amount")))
	if remaining < 0.005:
		remaining = 0

	method = None
	if payment_doc and payment_doc.get("mode_of_payment"):
		method = payment_doc.get("mode_of_payment")
	elif store_credit > 0 and payment_amount <= 0:
		method = "Store credit"
	elif payment_amount > 0:
		method = "Payment"
	else:
		method = "Fully paid"
	if store_credit > 0 and payment_amount > 0:
		method = _("Store credit + {0}").format(method)

	payment_date = None
	if payment_doc:
		payment_date = payment_doc.get("reference_date") or payment_doc.get("posting_date")
	payment_date = payment_date or invoice_doc.get("posting_date")

	return {
		"payment_entry": payment_doc.name if payment_doc else None,
		"payment_amount": flt(payment_amount),
		"payment_method": method,
		"payment_date": payment_date,
		"payment_date_display": formatdate(payment_date) if payment_date else "",
		"reference_no": payment_doc.get("reference_no") if payment_doc else None,
		"remaining_amount": flt(remaining),
		"receipt_reference": payment_doc.name if payment_doc else _paid_receipt_event_key(invoice_doc.name),
		"source": source,
	}


def _payment_entry_doc(payment_entry):
	if not payment_entry:
		return None
	if hasattr(payment_entry, "doctype"):
		return payment_entry
	if frappe.db.exists("Payment Entry", payment_entry):
		return frappe.get_doc("Payment Entry", payment_entry)
	return None


def _payment_entry_invoice_amount(payment_doc, invoice):
	if not payment_doc:
		return 0
	amount = 0
	for row in payment_doc.get("references", []):
		if row.get("reference_doctype") == "Sales Invoice" and row.get("reference_name") == invoice:
			amount += flt(row.get("allocated_amount"))
	return amount or flt(payment_doc.get("paid_amount") or payment_doc.get("received_amount"))


def _submitted_payment_amount(invoice):
	if not invoice or not frappe.db.exists("DocType", "Payment Entry Reference") or not frappe.db.exists("DocType", "Payment Entry"):
		return 0
	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": invoice, "parenttype": "Payment Entry"},
		fields=["parent", "allocated_amount"],
		limit_page_length=0,
	)
	if not rows:
		return 0
	payment_entries = sorted({row.get("parent") for row in rows if row.get("parent")})
	submitted = set(frappe.get_all("Payment Entry", filters={"name": ["in", payment_entries], "docstatus": 1}, pluck="name", limit_page_length=0))
	return sum(flt(row.get("allocated_amount")) for row in rows if row.get("parent") in submitted)


def _invoice_outstanding_amount(invoice_doc):
	invoice = invoice_doc.get("name")
	if invoice and frappe.db.has_column("Sales Invoice", "outstanding_amount"):
		value = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
		if value is not None:
			return flt(value)
	return flt(invoice_doc.get("outstanding_amount") or 0)


def _paid_receipt_event_key(invoice):
	return f"invoice_paid_receipt:{invoice}"


def _paid_receipt_already_logged(invoice):
	if not _notification_log_available() or not invoice:
		return False
	meta = frappe.get_meta("Notification Log")
	if meta.has_field("event_key"):
		if frappe.db.exists("Notification Log", {"event_key": _paid_receipt_event_key(invoice)}):
			return True
	subject = _("Queensland Art School - Payment receipt {0}").format(invoice)
	return bool(frappe.db.exists("Notification Log", {"document_type": "Sales Invoice", "document_name": invoice, "subject": subject}))


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
