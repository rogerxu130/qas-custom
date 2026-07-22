from __future__ import annotations

from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import escape_html

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.notifications.commands import (
	_create_notification_log,
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


DEFAULT_INQUIRY_NOTIFICATION_EMAIL = "rogerxu130@gmail.com"
INQUIRY_NOTIFICATION_CONFIG_KEY = "qas_inquiry_notification_email"
INQUIRY_ADMIN_EVENT_PREFIX = "inquiry_admin_new:"
SCHOOL_ADMIN_PORTAL_URL = "https://portal.queenslandartschool.com/school-admin"


def queue_inquiry_admin_notification(inquiry_doc):
	"""Queue one management email for every committed Inquiry insert."""
	inquiry = _document_value(inquiry_doc, "name")
	if not inquiry:
		return {"queued": False, "skipped": True, "reason": "Inquiry name is required."}

	event_key = inquiry_admin_event_key(inquiry)
	if _notification_event_exists(event_key):
		return {"queued": False, "skipped": True, "duplicate": True}

	try:
		frappe.enqueue(
			"qas_custom.modules.notifications.inquiry_admin_notifications.send_inquiry_admin_notification_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			job_id=event_key.replace(":", "-"),
			deduplicate=True,
			inquiry=inquiry,
		)
	except Exception:
		try:
			frappe.log_error(frappe.get_traceback(), "QAS Inquiry management email queue failed: {0}".format(inquiry))
		except Exception:
			pass
		return {"queued": False, "reason": "Management email could not be queued."}
	return {"queued": True, "event_key": event_key, "recipient": inquiry_notification_email()}


def send_inquiry_admin_notification_job(inquiry):
	if not inquiry or not frappe.db.exists("Inquiry", inquiry):
		return {"sent": False, "skipped": True, "reason": "Inquiry no longer exists."}

	event_key = inquiry_admin_event_key(inquiry)
	if _notification_event_exists(event_key):
		return {"sent": False, "skipped": True, "duplicate": True}

	doc = frappe.get_doc("Inquiry", inquiry)
	recipient = inquiry_notification_email()
	subject = _inquiry_admin_subject(doc)
	message = _inquiry_admin_message(doc)
	try:
		log_name = _create_notification_log(
			event_key=event_key,
			recipient={"email": recipient},
			subject=subject,
			message=message,
			document_type="Inquiry",
			document_name=inquiry,
		)
	except frappe.DuplicateEntryError:
		return {"sent": False, "skipped": True, "duplicate": True}

	if not log_name:
		return {
			"sent": False,
			"skipped": True,
			"reason": "Notification Log is unavailable; email was not sent without an idempotency reservation.",
		}

	_mark_notification_queued(log_name)
	if not outbound_email_enabled():
		reason = email_block_reason()
		_mark_notification_failed(log_name, reason)
		return {"sent": False, "skipped": True, "reason": reason, "notification_log": log_name}

	try:
		settings = get_invoice_settings()
		mail_kwargs = {
			"action": "inquiry_admin_new",
			"recipients": [recipient],
			"subject": subject,
			"message": message,
			"reference_doctype": "Inquiry",
			"reference_name": inquiry,
			"delayed": False,
		}
		school_email = str(settings.get("school_email") or "").strip().lower()
		if school_email:
			mail_kwargs["reply_to"] = school_email
		mail_result = sendmail_or_skip(**mail_kwargs)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(log_name, reason)
			return {"sent": False, "skipped": True, "reason": reason, "notification_log": log_name}

		_mark_notification_sent(log_name)
		return {"sent": True, "recipient": recipient, "notification_log": log_name}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS Inquiry management email failed: {0}".format(inquiry))
		_mark_notification_failed(log_name, "Email send failed.")
		return {"sent": False, "reason": "Email send failed.", "notification_log": log_name}


def inquiry_notification_email():
	configured = str(frappe.conf.get(INQUIRY_NOTIFICATION_CONFIG_KEY) or "").strip().lower()
	return configured or DEFAULT_INQUIRY_NOTIFICATION_EMAIL


def inquiry_admin_event_key(inquiry):
	digest = sha256(str(inquiry or "").encode()).hexdigest()[:24]
	return "{0}{1}".format(INQUIRY_ADMIN_EVENT_PREFIX, digest)


def _notification_event_exists(event_key):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _inquiry_admin_subject(doc):
	return _("New Inquiry: {0} — {1} — {2}").format(
		doc.name,
		_document_value(doc, "inquiry_type") or "Inquiry",
		_document_value(doc, "status") or "New",
	)


def _inquiry_admin_message(doc):
	rows = [
		(_("Inquiry"), doc.name),
		(_("Type"), _document_value(doc, "inquiry_type")),
		(_("Status"), _document_value(doc, "status")),
		(_("Source"), _document_value(doc, "source") or _document_value(doc, "webhook_source")),
		(_("Created"), _document_value(doc, "creation")),
		(_("Parent"), _document_value(doc, "parent")),
		(_("Student"), _document_value(doc, "student") or _document_value(doc, "submitted_student_name")),
		(_("Contact"), _document_value(doc, "contact_name")),
		(_("Email"), _document_value(doc, "contact_email")),
		(_("Phone"), _document_value(doc, "contact_phone")),
		(_("Campus"), _document_value(doc, "campus")),
		(_("Requested course"), _document_value(doc, "preferred_course")),
		(_("Appointment date"), _document_value(doc, "current_appointment_date") or _document_value(doc, "submitted_trial_date")),
		(_("Appointment time"), _document_value(doc, "current_appointment_time")),
		(_("Course Session"), _document_value(doc, "course_session") or _document_value(doc, "submitted_class_session")),
	]
	body = "".join(
		"<tr><th style='text-align:left;padding:7px 12px;border-bottom:1px solid #e5e7eb'>{0}</th>"
		"<td style='padding:7px 12px;border-bottom:1px solid #e5e7eb'>{1}</td></tr>".format(
			escape_html(str(label)),
			escape_html(_display(value)),
		)
		for label, value in rows
	)
	portal_url = _school_admin_portal_url()
	return (
		"<p>{0}</p><table style='border-collapse:collapse'>{1}</table>"
		"<p><a href='{2}'>{3}</a></p>"
	).format(
		escape_html(_("A new Inquiry has been created.")),
		body,
		escape_html(portal_url),
		escape_html(_("Open School Admin Inquiries")),
	)


def _school_admin_portal_url():
	base_url = str(
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or ""
	).strip().rstrip("/")
	return "{0}/school-admin".format(base_url) if base_url else SCHOOL_ADMIN_PORTAL_URL


def _document_value(doc, fieldname):
	if hasattr(doc, "get"):
		return doc.get(fieldname)
	return getattr(doc, fieldname, None)


def _display(value):
	return str(value) if value not in (None, "") else "-"
