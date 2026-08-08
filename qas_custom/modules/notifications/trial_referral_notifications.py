from __future__ import annotations

from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import escape_html, flt

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.presentation import DEFAULT_PARENT_PORTAL_URL
from qas_custom.modules.notifications.commands import (
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
)
from qas_custom.modules.trial_referrals import (
	referral_conversion_reward_amount,
	trial_referral_discount_amount,
	verified_referral_parent,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


EVENT_PREFIXES = {
	"trial_discount": "trial_referral_discount:",
	"conversion_reward": "trial_referral_reward:",
}


def queue_referral_trial_discount_notification(inquiry: str):
	return _queue_referral_notification("trial_discount", inquiry)


def queue_referral_conversion_reward_notification(inquiry: str):
	return _queue_referral_notification("conversion_reward", inquiry)


def _queue_referral_notification(event_kind: str, inquiry: str):
	if event_kind not in EVENT_PREFIXES:
		return {"queued": False, "skipped": True, "reason": "Unsupported referral email event."}
	if not inquiry or not frappe.db.exists("Inquiry", inquiry):
		return {"queued": False, "skipped": True, "reason": "Inquiry was not found."}

	event_key = referral_notification_event_key(event_kind, inquiry)
	try:
		if _notification_event_exists(event_key):
			return {"queued": False, "skipped": True, "duplicate": True, "event_key": event_key}
		context = _build_referral_context(event_kind, inquiry)
		if not context.get("is_current"):
			return {"queued": False, "skipped": True, "reason": "Referral notification is no longer current.", "event_key": event_key}
		subject = _referral_subject(context)
		message = _referral_message(context)
		try:
			log_name = _reserve_notification_event(event_key, context, subject, message)
		except frappe.DuplicateEntryError:
			return {"queued": False, "skipped": True, "duplicate": True, "event_key": event_key}
		if not log_name:
			return {
				"queued": False,
				"skipped": True,
				"reason": "Notification Log is unavailable; email was not queued without an idempotency reservation.",
				"event_key": event_key,
			}
		if not context["recipient"].get("email"):
			_mark_notification_failed(log_name, "No referring parent email found.")
			return {"queued": False, "reason": "No referring parent email found.", "notification_log": log_name}

		_mark_notification_queued(log_name)
		if not outbound_email_enabled():
			reason = email_block_reason()
			_mark_notification_failed(log_name, reason)
			return {"queued": False, "skipped": True, "reason": reason, "notification_log": log_name}
		frappe.enqueue(
			"qas_custom.modules.notifications.trial_referral_notifications.send_referral_notification_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			job_id=event_key.replace(":", "-"),
			deduplicate=True,
			event_kind=event_kind,
			inquiry=inquiry,
			notification_log=log_name,
		)
		return {
			"queued": True,
			"recipient": context["recipient"]["email"],
			"notification_log": log_name,
			"event_key": event_key,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS referral email queue failed: {0}".format(inquiry))
		return {"queued": False, "reason": "Referral email could not be queued.", "event_key": event_key}


def send_referral_notification_job(*, event_kind: str, inquiry: str, notification_log: str | None = None):
	if _notification_log_sent(notification_log):
		return {"sent": False, "skipped": True, "duplicate": True}
	if not outbound_email_enabled():
		reason = email_block_reason()
		_mark_notification_failed(notification_log, reason)
		return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}
	try:
		context = _build_referral_context(event_kind, inquiry)
		if not context.get("is_current"):
			reason = "Referral notification is no longer current."
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}
		recipient = context["recipient"].get("email")
		if not recipient:
			_mark_notification_failed(notification_log, "No referring parent email found.")
			return {"sent": False, "reason": "No referring parent email found.", "notification_log": notification_log}
		subject = _referral_subject(context)
		message = _referral_message(context)
		_refresh_notification_log(notification_log, context, subject, message)
		kwargs = {
			"action": "trial_referral_{0}".format(event_kind),
			"recipients": [recipient],
			"subject": subject,
			"message": message,
			"reference_doctype": "Inquiry",
			"reference_name": inquiry,
			"delayed": False,
		}
		if context.get("school_email"):
			kwargs["reply_to"] = context["school_email"]
		result = sendmail_or_skip(**kwargs)
		if result and result.get("skipped"):
			reason = result.get("reason") or email_block_reason()
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}
		_mark_notification_sent(notification_log)
		return {"sent": True, "recipient": recipient, "notification_log": notification_log}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS referral email failed: {0}".format(inquiry))
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed.", "notification_log": notification_log}


def referral_notification_event_key(event_kind: str, inquiry: str) -> str:
	if event_kind not in EVENT_PREFIXES:
		raise ValueError("Unsupported referral email event.")
	digest = sha256("\x1f".join((event_kind, str(inquiry or ""))).encode()).hexdigest()[:24]
	return "{0}{1}".format(EVENT_PREFIXES[event_kind], digest)


def _build_referral_context(event_kind: str, inquiry: str) -> dict:
	doc = frappe.get_doc("Inquiry", inquiry)
	parent = verified_referral_parent(doc)
	recipient = _parent_recipient(parent)
	settings = get_invoice_settings()
	if event_kind == "trial_discount":
		invoice = frappe.db.get_value("Sales Invoice", doc.get("trial_invoice"), ["name", "docstatus", "status"], as_dict=True) if doc.get("trial_invoice") else None
		is_current = bool(invoice and int(invoice.get("docstatus") or 0) == 1 and str(invoice.get("status") or "").lower() != "cancelled")
		amount = trial_referral_discount_amount()
	else:
		ledger = _referral_reward(doc.name)
		is_current = bool(doc.get("status") == "Converted" and ledger)
		amount = flt((ledger or {}).get("credit_amount") or referral_conversion_reward_amount())
	return {
		"event_kind": event_kind,
		"inquiry": doc.name,
		"recipient": recipient,
		"amount": amount,
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_email": str(settings.get("school_email") or "").strip().lower(),
		"portal_url": _parent_portal_url("/credit"),
		"is_current": is_current,
	}


def _referral_subject(context: dict) -> str:
	if context["event_kind"] == "trial_discount":
		return _("Thank you for your Queensland Art School referral")
	return _("Your Queensland Art School referral reward")


def _referral_message(context: dict) -> str:
	school_name = escape_html(context["school_name"])
	amount = escape_html(frappe.format_value(context["amount"], {"fieldtype": "Currency"}))
	if context["event_kind"] == "trial_discount":
		headline = _("Thank you for your referral")
		body = _(
			"A family you referred has received a {0} discount on their trial class. "
			"If they enrol in a full-term class, we will add {0} Store Credit to your account."
		).format(amount)
	else:
		headline = _("Your referral reward is ready")
		body = _(
			"A family you referred has now enrolled in a full-term class. "
			"We have added {0} Store Credit to your account."
		).format(amount)
	return """
		<div style=\"margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;\">
			<div style=\"max-width:640px;margin:0 auto;padding:24px;\">
				<div style=\"background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;padding:24px;\">
					<p style=\"margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#e85f47;\">{school}</p>
					<h1 style=\"margin:0 0 16px;font-size:24px;\">{headline}</h1>
					<p style=\"margin:0 0 20px;font-size:16px;line-height:1.55;\">{body}</p>
					<a href=\"{portal}\" style=\"display:inline-block;background:#ef654d;color:#ffffff;text-decoration:none;padding:12px 16px;border-radius:8px;font-weight:700;\">View Parent Portal</a>
				</div>
			</div>
		</div>
	""".format(
		school=school_name,
		headline=escape_html(headline),
		body=escape_html(body),
		portal=escape_html(context["portal_url"]),
	)


def _parent_recipient(parent: str) -> dict:
	if not parent:
		return {"email": "", "for_user": None}
	fields = ["name"]
	for fieldname in ["linked_user", "email", "email_id", "contact_email"]:
		if frappe.db.has_column("Parent", fieldname):
			fields.append(fieldname)
	row = frappe.db.get_value("Parent", parent, fields, as_dict=True) or {}
	linked_user = row.get("linked_user")
	email = next((row.get(field) for field in ["email", "email_id", "contact_email"] if row.get(field)), None)
	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user
	return {"email": str(email or "").strip().lower(), "for_user": linked_user}


def _referral_reward(inquiry: str):
	return frappe.db.get_value(
		"QAS Store Credit Ledger",
		{
			"transaction_type": "Referral Reward",
			"source_doctype": "Inquiry",
			"source_document": inquiry,
		},
		["name", "credit_amount"],
		as_dict=True,
	)


def _parent_portal_url(path: str) -> str:
	base = frappe.conf.get("qas_parent_portal_url") or frappe.conf.get("parent_portal_url") or DEFAULT_PARENT_PORTAL_URL
	return "{0}{1}".format(str(base).rstrip("/"), path)


def _reserve_notification_event(event_key: str, context: dict, subject: str, message: str):
	if not _notification_log_available():
		return None
	with frappe.cache.lock("qas-trial-referral-email:{0}".format(event_key), timeout=30, blocking_timeout=10):
		if _notification_event_exists(event_key):
			raise frappe.DuplicateEntryError
		log = frappe.new_doc("Notification Log")
		log.subject = subject
		log.type = "Alert"
		log.email_content = message
		log.document_type = "Inquiry"
		log.document_name = context["inquiry"]
		log.from_user = frappe.session.user
		if log.meta.has_field("for_user"):
			log.for_user = context["recipient"].get("for_user") or frappe.session.user
		for fieldname, value in {
			"event_key": event_key,
			"email_to": context["recipient"].get("email"),
			"recipient_email": context["recipient"].get("email"),
		}.items():
			if log.meta.has_field(fieldname):
				setattr(log, fieldname, value)
		if not log.meta.has_field("event_key"):
			log.document_name = event_key
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)
		return log.name


def _notification_event_exists(event_key: str) -> bool:
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	fieldname = "event_key" if meta.has_field("event_key") else "document_name"
	return bool(frappe.db.exists("Notification Log", {fieldname: event_key}))


def _notification_log_sent(notification_log: str | None) -> bool:
	if not notification_log or not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	fields = [fieldname for fieldname in ["status", "delivery_status", "email_status"] if meta.has_field(fieldname)]
	if not fields:
		return False
	row = frappe.db.get_value("Notification Log", notification_log, fields, as_dict=True) or {}
	return any(row.get(fieldname) == "Sent" for fieldname in fields)


def _refresh_notification_log(notification_log: str | None, context: dict, subject: str, message: str):
	if not notification_log or not _notification_log_available():
		return
	meta = frappe.get_meta("Notification Log")
	values = {"subject": subject, "email_content": message}
	for fieldname, value in {
		"for_user": context["recipient"].get("for_user"),
		"email_to": context["recipient"].get("email"),
		"recipient_email": context["recipient"].get("email"),
	}.items():
		if meta.has_field(fieldname):
			values[fieldname] = value
	frappe.db.set_value("Notification Log", notification_log, values, update_modified=False)
