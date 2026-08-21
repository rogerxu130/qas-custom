from __future__ import annotations

from datetime import datetime
from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import cint, escape_html, formatdate, get_time, getdate

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.notifications.commands import (
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


ACTIVE_SCHOOL_VISIT_STATUSES = {"Booked", "Rescheduled"}
CONFIG_KEY = "qas_school_visit_parent_email_enabled"
EVENT_PREFIXES = {
	"booked": "school_visit_parent_booked:",
	"rescheduled": "school_visit_parent_rescheduled:",
}


def queue_school_visit_parent_booking_change(inquiry_doc, old_doc=None):
	"""Queue one parent email for a meaningful School Visit booking change."""
	event_kind = classify_school_visit_booking_change(inquiry_doc, old_doc)
	if not event_kind:
		return {"queued": False, "skipped": True, "reason": "No School Visit parent email is required."}
	if not school_visit_parent_email_enabled():
		return {"queued": False, "skipped": True, "reason": "Automatic School Visit emails are disabled."}
	if not outbound_email_enabled():
		return {"queued": False, "skipped": True, "reason": email_block_reason()}

	appointment_signature = school_visit_signature(inquiry_doc)
	event_key = school_visit_parent_event_key(event_kind, inquiry_doc.name, appointment_signature)
	if _notification_event_exists(event_key):
		return {"queued": False, "skipped": True, "duplicate": True}

	frappe.enqueue(
		"qas_custom.modules.notifications.school_visit_parent_notifications.send_school_visit_parent_email_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=event_key.replace(":", "-"),
		deduplicate=True,
		inquiry=inquiry_doc.name,
		appointment_signature=appointment_signature,
		event_kind=event_kind,
	)
	return {"queued": True, "event_kind": event_kind}


def classify_school_visit_booking_change(inquiry_doc, old_doc=None):
	if not _eligible_school_visit(inquiry_doc):
		return None
	if not old_doc or not _eligible_school_visit(old_doc):
		return "booked"
	if school_visit_signature(inquiry_doc) != school_visit_signature(old_doc):
		return "rescheduled"
	return None


def send_school_visit_parent_email_job(inquiry, appointment_signature, event_kind):
	if not school_visit_parent_email_enabled():
		return _skipped("Automatic School Visit emails are disabled.")
	if not outbound_email_enabled():
		return _skipped(email_block_reason())
	if event_kind not in EVENT_PREFIXES:
		return _skipped("Unsupported automatic School Visit parent email event.")

	doc = frappe.get_doc("Inquiry", inquiry)
	if not _eligible_school_visit(doc):
		return _skipped("The School Visit is no longer a valid active booking.")
	if school_visit_signature(doc) != appointment_signature:
		return _skipped("The queued School Visit appointment has been superseded.")

	event_key = school_visit_parent_event_key(event_kind, inquiry, appointment_signature)
	if _notification_event_exists(event_key):
		return _skipped("This automatic School Visit parent email was already recorded.")
	return _send_automatic_school_visit_parent_email(doc, event_kind, event_key)


def school_visit_parent_email_enabled():
	value = frappe.conf.get(CONFIG_KEY)
	return True if value is None else cint(value) != 0


def school_visit_signature(inquiry_doc):
	identity = "\x1f".join(
		(
			str(_doc_value(inquiry_doc, "campus") or "").strip(),
			_normalize_date(_doc_value(inquiry_doc, "current_appointment_date")),
			_normalize_time(_doc_value(inquiry_doc, "current_appointment_time")),
		)
	)
	return sha256(identity.encode()).hexdigest()[:24]


def school_visit_parent_event_key(event_kind, inquiry, appointment_signature):
	identity = "\x1f".join((str(inquiry or ""), str(appointment_signature or ""), str(event_kind or "")))
	digest = sha256(identity.encode()).hexdigest()[:24]
	return "{0}{1}".format(EVENT_PREFIXES[event_kind], digest)


def _eligible_school_visit(inquiry_doc):
	if not inquiry_doc or _doc_value(inquiry_doc, "inquiry_type") != "School Visit":
		return False
	if (_doc_value(inquiry_doc, "status") or "").strip() not in ACTIVE_SCHOOL_VISIT_STATUSES:
		return False
	return all(
		_doc_value(inquiry_doc, fieldname)
		for fieldname in ("campus", "current_appointment_date", "current_appointment_time", "contact_email")
	)


def _school_visit_parent_context(inquiry_doc):
	campus_name = _doc_value(inquiry_doc, "campus")
	campus = (
		frappe.db.get_value("Campus", campus_name, ["name", "address"], as_dict=True)
		if campus_name
		else None
	)
	parent = _doc_value(inquiry_doc, "parent")
	parent_row = (
		frappe.db.get_value("Parent", parent, ["linked_user"], as_dict=True) or {}
		if parent
		else {}
	)
	settings = get_invoice_settings()
	context = {
		"recipient": {
			"email": str(_doc_value(inquiry_doc, "contact_email") or "").strip().lower(),
			"for_user": parent_row.get("linked_user"),
			"parent": parent,
		},
		"parent_name": _doc_value(inquiry_doc, "contact_name") or "Parent",
		"campus": campus_name,
		"campus_address": (campus or {}).get("address"),
		"date_display": (
			formatdate(_doc_value(inquiry_doc, "current_appointment_date"), "d MMMM yyyy")
			if _doc_value(inquiry_doc, "current_appointment_date")
			else None
		),
		"time_display": _display_time(_doc_value(inquiry_doc, "current_appointment_time")),
		"duration_minutes": 15,
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_email": settings.get("school_email"),
		"school_phone": settings.get("school_phone"),
	}
	labels = {
		"recipient": "parent email",
		"campus": "campus",
		"campus_address": "campus address",
		"date_display": "appointment date",
		"time_display": "appointment time",
		"school_email": "school reply email",
		"school_phone": "school phone",
	}
	missing = [
		label
		for key, label in labels.items()
		if not context.get(key) or (key == "recipient" and not context[key].get("email"))
	]
	if missing:
		frappe.throw(_("Cannot send School Visit email. Missing: {0}.").format(", ".join(missing)))
	return context


def _send_automatic_school_visit_parent_email(inquiry_doc, event_kind, event_key):
	try:
		context = _school_visit_parent_context(inquiry_doc)
	except Exception as exc:
		_reserve_failed_event(event_key, inquiry_doc.name, str(exc))
		return {"sent": False, "reason": str(exc)}

	subject = _school_visit_email_subject(context, event_kind)
	message = _school_visit_email_message(context, event_kind)
	try:
		log_name = _reserve_notification_event(
			event_key,
			context["recipient"],
			subject,
			message,
			inquiry_doc.name,
		)
	except frappe.DuplicateEntryError:
		return _skipped("This automatic School Visit parent email was already recorded.")
	if not log_name:
		return _skipped("Notification Log is unavailable; email was not sent without an idempotency reservation.")

	_mark_notification_queued(log_name)
	try:
		mail_result = sendmail_or_skip(
			action="school_visit_parent_{0}".format(event_kind),
			recipients=[context["recipient"]["email"]],
			subject=subject,
			message=message,
			reference_doctype="Inquiry",
			reference_name=inquiry_doc.name,
			reply_to=context["school_email"],
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(log_name, reason)
			return _skipped(reason)
		_mark_notification_sent(log_name)
		return {"sent": True, "recipient": context["recipient"]["email"]}
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"QAS automatic School Visit parent email failed: {0}".format(inquiry_doc.name),
		)
		_mark_notification_failed(log_name, "Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def _school_visit_email_subject(context, event_kind):
	label = _("School Visit Booked") if event_kind == "booked" else _("School Visit Rescheduled")
	return _("{0}: {1} — {2}").format(label, context["campus"], context["date_display"])


def _school_visit_email_message(context, event_kind):
	def value(key):
		return escape_html(context.get(key) or "")

	heading = _("School Visit Booked") if event_kind == "booked" else _("School Visit Rescheduled")
	intro = (
		_("Your School Visit has been booked successfully.")
		if event_kind == "booked"
		else _("Your School Visit has been rescheduled. Please use the updated details below.")
	)
	duration = _("Please allow approximately {0} minutes for your visit.").format(context["duration_minutes"])
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
							<tr><td style="padding:9px 0;color:#64748b;">Campus</td><td style="padding:9px 0;text-align:right;font-weight:700;">{campus}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Address</td><td style="padding:9px 0;text-align:right;font-weight:700;">{campus_address}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Date</td><td style="padding:9px 0;text-align:right;font-weight:700;">{date_display}</td></tr>
							<tr><td style="padding:9px 0;color:#64748b;">Time</td><td style="padding:9px 0;text-align:right;font-weight:700;">{time_display}</td></tr>
						</table>
						<p style="margin:0 0 18px;padding:12px 14px;border-radius:8px;background:#fff7ed;font-size:15px;line-height:1.5;color:#9a3412;font-weight:700;">{duration}</p>
						<p style="margin:0;font-size:15px;line-height:1.5;color:#475569;">If you are unable to attend or need to make changes, please reply to this email or call {school_name} on {school_phone}.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=value("school_name"),
		heading=escape_html(heading),
		parent_name=value("parent_name"),
		intro=escape_html(intro),
		campus=value("campus"),
		campus_address=value("campus_address").replace("\n", "<br>"),
		date_display=value("date_display"),
		time_display=value("time_display"),
		duration=escape_html(duration),
		school_phone=value("school_phone"),
	)


def _notification_event_exists(event_key):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _reserve_notification_event(event_key, recipient, subject, message, inquiry):
	if not _notification_log_available():
		return None
	with frappe.cache.lock("qas-school-visit-parent-email:{0}".format(event_key), timeout=30, blocking_timeout=10):
		if _notification_event_exists(event_key):
			raise frappe.DuplicateEntryError
		log = frappe.new_doc("Notification Log")
		log.subject = subject
		log.type = "Alert"
		log.email_content = message
		log.document_type = "Inquiry"
		log.document_name = inquiry
		log.from_user = frappe.session.user
		if log.meta.has_field("for_user"):
			log.for_user = recipient.get("for_user") or frappe.session.user
		for fieldname, fieldvalue in {
			"event_key": event_key,
			"email_to": recipient.get("email"),
			"recipient_email": recipient.get("email"),
			"reference_doctype": "Inquiry",
			"reference_name": inquiry,
		}.items():
			if log.meta.has_field(fieldname):
				setattr(log, fieldname, fieldvalue)
		if not log.meta.has_field("event_key"):
			log.document_name = event_key
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)
		return log.name


def _reserve_failed_event(event_key, inquiry, reason):
	try:
		log_name = _reserve_notification_event(
			event_key,
			{"email": ""},
			_("Automatic School Visit parent email could not be prepared"),
			reason,
			inquiry,
		)
		_mark_notification_failed(log_name, reason)
	except frappe.DuplicateEntryError:
		pass


def _normalize_date(value):
	return str(getdate(value)) if value else ""


def _normalize_time(value):
	return str(get_time(value)) if value not in (None, "") else ""


def _display_time(value):
	if value in (None, ""):
		return ""
	return datetime.combine(getdate(), get_time(value)).strftime("%I:%M %p").lstrip("0")


def _doc_value(doc, fieldname):
	if hasattr(doc, "get"):
		return doc.get(fieldname)
	return getattr(doc, fieldname, None)


def _skipped(reason):
	return {"sent": False, "skipped": True, "reason": reason}
