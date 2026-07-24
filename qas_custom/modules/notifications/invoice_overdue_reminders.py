from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, date_diff, escape_html, flt, get_datetime, get_datetime_in_timezone, get_system_timezone, getdate

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.presentation import build_parent_invoice_context, parent_portal_invoice_link
from qas_custom.modules.notifications.commands import (
	_invoice_email_bank_details,
	_invoice_email_greeting,
	_invoice_email_item_row,
	_invoice_email_portal_action,
	_invoice_outstanding_amount,
	_invoice_pdf_attachment,
	_invoice_portal_links_enabled,
	_invoice_recipient,
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


BRISBANE_TIMEZONE = "Australia/Brisbane"
CONFIG_KEY = "qas_overdue_invoice_reminders_enabled"
EVENT_PREFIX = "invoice_overdue_reminder:"
REMINDER_INTERVAL_DAYS = 3
MAX_REMINDER_ATTEMPTS = 5
SCHEDULER_BATCH_SIZE = 100


def run_overdue_invoice_reminders(today=None):
	if not overdue_invoice_reminders_enabled():
		return {"skipped": True, "reason": "Automatic overdue invoice reminders are disabled."}
	if not outbound_email_enabled():
		return {"skipped": True, "reason": email_block_reason()}
	if not _notification_log_available():
		return {"skipped": True, "reason": "Notification Log is unavailable."}

	today = _brisbane_date(today)
	candidates = _get_overdue_invoice_candidates(today)
	history = _get_reminder_history([row.get("name") for row in candidates])
	result = {"eligible": len(candidates), "queued": 0, "skipped": 0, "failed": 0}

	for candidate in candidates:
		try:
			attempts = history.get(candidate.get("name"), [])
			sequence = next_reminder_sequence(candidate, attempts, today)
			if not sequence:
				result["skipped"] += 1
				continue
			queue_result = _queue_overdue_invoice_reminder(candidate.get("name"), sequence, today)
			if queue_result.get("queued"):
				result["queued"] += 1
			elif queue_result.get("failed"):
				result["failed"] += 1
			else:
				result["skipped"] += 1
		except Exception:
			result["failed"] += 1
			frappe.log_error(frappe.get_traceback(), "QAS overdue invoice reminder queue failed")
	return result


def send_overdue_invoice_reminder_job(invoice, sequence, event_key, notification_log, today=None):
	if not overdue_invoice_reminders_enabled():
		return _fail_reserved_reminder(notification_log, "Automatic overdue invoice reminders are disabled.")
	if not outbound_email_enabled():
		return _fail_reserved_reminder(notification_log, email_block_reason())
	if _notification_was_sent(notification_log):
		return {"sent": False, "skipped": True, "duplicate": True, "reason": "This overdue reminder was already sent."}

	today = _brisbane_date(today)
	doc = frappe.get_doc("Sales Invoice", invoice)
	reason = _invoice_ineligible_reason(doc, today)
	if reason:
		return _fail_reserved_reminder(notification_log, reason, skipped=True)

	recipient = _invoice_recipient(doc)
	if not recipient.get("email"):
		return _fail_reserved_reminder(notification_log, "No parent email found.")

	outstanding = _invoice_outstanding_amount(doc)
	days_overdue = date_diff(today, getdate(doc.get("due_date")))
	subject = overdue_reminder_subject(doc)
	message = overdue_reminder_email_message(doc, days_overdue=days_overdue, outstanding_amount=outstanding)
	_refresh_notification_log(notification_log, recipient, subject, message)
	settings = get_invoice_settings()

	try:
		mail_result = sendmail_or_skip(
			action="parent_invoice_overdue_reminder",
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=doc.name,
			reply_to=settings.get("school_email") or None,
			delayed=False,
			attachments=[
				_invoice_pdf_attachment(
					doc.name,
					payable_amount=outstanding,
				)
			],
		)
		if mail_result and mail_result.get("skipped"):
			return _fail_reserved_reminder(
				notification_log,
				mail_result.get("reason") or email_block_reason(),
				skipped=True,
			)
		_mark_notification_sent(notification_log)
		return {
			"sent": True,
			"recipient": recipient["email"],
			"invoice": doc.name,
			"sequence": cint(sequence),
			"event_key": event_key,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS overdue invoice reminder failed: {0}".format(doc.name))
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed.", "invoice": doc.name}


def next_reminder_sequence(invoice, attempts, today):
	eligibility = overdue_reminder_eligibility(invoice, attempts, today)
	return eligibility.get("sequence") if eligibility.get("eligible") else None


def overdue_reminder_eligibility(invoice, attempts=None, today=None):
	today = _brisbane_date(today)
	due_date = getdate(invoice.get("due_date")) if invoice.get("due_date") else None
	if cint(invoice.get("docstatus")) != 1:
		return _ineligible("not_submitted", _("Invoice is not submitted."))
	if cint(invoice.get("is_return")):
		return _ineligible("return_invoice", _("Return invoices do not receive overdue reminders."))
	if not due_date:
		return _ineligible("missing_due_date", _("Invoice does not have a due date."))
	if due_date >= today:
		return _ineligible("not_overdue", _("Invoice is not overdue."))
	if flt(invoice.get("outstanding_amount")) <= 0.005:
		return _ineligible("no_outstanding", _("Invoice no longer has an outstanding balance."))

	attempt_dates = sorted(
		(value for value in (_attempt_datetime(row) for row in (attempts or [])) if value),
		reverse=True,
	)
	attempt_count = len(attempt_dates)
	if attempt_count >= MAX_REMINDER_ATTEMPTS:
		return _ineligible(
			"maximum_attempts",
			_("The maximum of {0} overdue reminders has been reached.").format(MAX_REMINDER_ATTEMPTS),
			attempt_count=attempt_count,
			last_reminder_at=attempt_dates[0] if attempt_dates else None,
		)
	if not attempt_dates:
		return {
			"eligible": True,
			"reason_code": None,
			"reason": None,
			"attempt_count": 0,
			"sequence": 1,
			"last_reminder_at": None,
			"days_until_eligible": 0,
		}

	last_reminder_at = attempt_dates[0]
	last_attempt_date = _system_datetime_to_brisbane(last_reminder_at).date()
	days_since_last_attempt = date_diff(today, last_attempt_date)
	if days_since_last_attempt < REMINDER_INTERVAL_DAYS:
		return _ineligible(
			"recently_reminded",
			_("This invoice was reminded within the last {0} days.").format(REMINDER_INTERVAL_DAYS),
			attempt_count=attempt_count,
			last_reminder_at=last_reminder_at,
			days_until_eligible=REMINDER_INTERVAL_DAYS - days_since_last_attempt,
		)
	return {
		"eligible": True,
		"reason_code": None,
		"reason": None,
		"attempt_count": attempt_count,
		"sequence": attempt_count + 1,
		"last_reminder_at": last_reminder_at,
		"days_until_eligible": 0,
	}


def overdue_reminder_event_key(invoice, due_date, sequence):
	key = "{0}{1}:{2}:{3}".format(
		EVENT_PREFIX,
		str(invoice or "").strip(),
		getdate(due_date).isoformat(),
		cint(sequence),
	)
	if len(key) <= 140:
		return key
	from hashlib import sha256

	identity = "\x1f".join((str(invoice or ""), str(due_date or ""), str(sequence or "")))
	return "{0}{1}:{2}".format(EVENT_PREFIX, cint(sequence), sha256(identity.encode()).hexdigest()[:32])


def overdue_reminder_subject(invoice_doc):
	return _("Payment reminder: Invoice {0} is overdue").format(invoice_doc.name)


def overdue_reminder_email_message(invoice_doc, *, days_overdue, outstanding_amount):
	context = build_parent_invoice_context(
		invoice_doc,
		payable_amount=outstanding_amount,
		invoice_link=parent_portal_invoice_link(invoice_doc.name),
		include_portal_link=_invoice_portal_links_enabled(),
	)
	settings = get_invoice_settings()
	context.update(
		{
			"school_email": settings.get("school_email") or "",
			"school_phone": settings.get("school_phone") or "",
		}
	)
	rows = "\n".join(_invoice_email_item_row(item) for item in context.get("items", []))
	if not rows:
		rows = """<tr><td colspan="3" style="padding:12px;color:#64748b;">Invoice details are included in the attached PDF.</td></tr>"""
	bank_details = _invoice_email_bank_details(context)
	contact_parts = [value for value in [context.get("school_email"), context.get("school_phone")] if value]
	contact_line = ""
	if contact_parts:
		contact_line = _("If you need assistance, please contact us at {0}.").format(
			escape_html(" or ".join(contact_parts))
		)

	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Payment reminder</h1>
						<p style="margin:8px 0 0;color:#cbd5e1;">Invoice {invoice}</p>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">
							This invoice is now <strong>{days_overdue} {day_label} overdue</strong> and still has an outstanding balance.
							Please arrange payment at your earliest convenience.
						</p>

						<table style="width:100%;border-collapse:collapse;margin:0 0 18px;">
							<tr>
								<td style="padding:10px 0;color:#64748b;">Original due date</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">{due_date}</td>
							</tr>
							<tr>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;font-size:17px;font-weight:700;">Outstanding amount</td>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;text-align:right;font-size:20px;font-weight:800;color:#e85f47;">AUD ${outstanding:.2f}</td>
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

						<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;">
							Please use invoice <strong>{invoice}</strong> as your payment reference.
						</p>
						{bank_details}
						{portal_action}
						<p style="margin:0 0 8px;font-size:14px;line-height:1.5;color:#64748b;">
							If you have already paid, no further action is needed.
						</p>
						<p style="margin:0;font-size:14px;line-height:1.5;color:#64748b;">{contact_line}</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		invoice=escape_html(invoice_doc.name),
		greeting=_invoice_email_greeting(context),
		days_overdue=cint(days_overdue),
		day_label="day" if cint(days_overdue) == 1 else "days",
		due_date=escape_html(context.get("due_date") or "-"),
		outstanding=flt(outstanding_amount),
		rows=rows,
		bank_details=bank_details,
		portal_action=_invoice_email_portal_action(context),
		contact_line=contact_line,
	)


def overdue_invoice_reminders_enabled():
	value = frappe.conf.get(CONFIG_KEY)
	return True if value is None else cint(value) != 0


def queue_overdue_invoice_reminder(invoice, today=None):
	today = _brisbane_date(today)
	doc = frappe.get_doc("Sales Invoice", invoice)
	history = _get_reminder_history([doc.name]).get(doc.name, [])
	eligibility = overdue_reminder_eligibility(doc, history, today)
	if not eligibility.get("eligible"):
		return {
			"queued": False,
			"skipped": True,
			"invoice": doc.name,
			**eligibility,
		}
	recipient = _invoice_recipient(doc)
	if not recipient.get("email"):
		return {
			"queued": False,
			"skipped": True,
			"invoice": doc.name,
			**_ineligible(
				"missing_recipient",
				_("No parent email found."),
				attempt_count=eligibility.get("attempt_count"),
				last_reminder_at=eligibility.get("last_reminder_at"),
			),
		}
	return _queue_overdue_invoice_reminder(doc.name, eligibility.get("sequence"), today)


def _queue_overdue_invoice_reminder(invoice, sequence, today):
	doc = frappe.get_doc("Sales Invoice", invoice)
	reason = _invoice_ineligible_reason(doc, today)
	if reason:
		return {"queued": False, "skipped": True, "reason": reason}

	outstanding = _invoice_outstanding_amount(doc)
	days_overdue = date_diff(today, getdate(doc.get("due_date")))
	recipient = _invoice_recipient(doc)
	subject = overdue_reminder_subject(doc)
	message = overdue_reminder_email_message(doc, days_overdue=days_overdue, outstanding_amount=outstanding)
	event_key = overdue_reminder_event_key(doc.name, doc.get("due_date"), sequence)
	try:
		log_name = _reserve_notification_event(event_key, doc, recipient, subject, message)
	except frappe.DuplicateEntryError:
		return {"queued": False, "skipped": True, "duplicate": True}
	if not log_name:
		return {"queued": False, "skipped": True, "reason": "Notification Log is unavailable."}
	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {"queued": False, "failed": True, "reason": "No parent email found."}

	_mark_notification_queued(log_name)
	try:
		frappe.enqueue(
			"qas_custom.modules.notifications.invoice_overdue_reminders.send_overdue_invoice_reminder_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			job_id=event_key.replace(":", "-"),
			deduplicate=True,
			invoice=doc.name,
			sequence=cint(sequence),
			event_key=event_key,
			notification_log=log_name,
		)
	except Exception:
		_mark_notification_failed(log_name, "Email job could not be queued.")
		raise
	return {"queued": True, "event_key": event_key, "notification_log": log_name}


def _get_overdue_invoice_candidates(today):
	filters = {
		"docstatus": 1,
		"due_date": ["<", today],
		"outstanding_amount": [">", 0.005],
	}
	fields = ["name", "due_date", "outstanding_amount", "docstatus"]
	if frappe.db.has_column("Sales Invoice", "is_return"):
		filters["is_return"] = 0
		fields.append("is_return")
	return frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=fields,
		order_by="due_date asc, name asc",
		limit_page_length=SCHEDULER_BATCH_SIZE,
	)


def _get_reminder_history(invoice_names):
	invoice_names = list({name for name in invoice_names if name})
	history = {name: [] for name in invoice_names}
	if not invoice_names or not _notification_log_available():
		return history

	meta = frappe.get_meta("Notification Log")
	if meta.has_field("event_key"):
		rows = frappe.get_all(
			"Notification Log",
			filters={
				"document_type": "Sales Invoice",
				"document_name": ["in", invoice_names],
				"event_key": ["like", "{0}%".format(EVENT_PREFIX)],
			},
			fields=["document_name", "creation", "event_key"],
			order_by="creation asc",
			limit_page_length=0,
		)
		for row in rows:
			if row.get("document_name") in history:
				history[row.get("document_name")].append(row)
		return history

	rows = frappe.get_all(
		"Notification Log",
		filters={"document_name": ["like", "{0}%".format(EVENT_PREFIX)]},
		fields=["document_name", "creation"],
		order_by="creation asc",
		limit_page_length=0,
	)
	for row in rows:
		invoice = _invoice_from_fallback_event_key(row.get("document_name"))
		if invoice in history:
			history[invoice].append(row)
	return history


def _reserve_notification_event(event_key, invoice_doc, recipient, subject, message):
	if not _notification_log_available():
		return None
	lock_name = "qas-overdue-invoice-email:{0}".format(event_key)
	with frappe.cache.lock(lock_name, timeout=30, blocking_timeout=10):
		if _notification_event_exists(event_key):
			raise frappe.DuplicateEntryError

		log = frappe.new_doc("Notification Log")
		log.subject = subject
		log.type = "Alert"
		log.email_content = message
		log.document_type = "Sales Invoice"
		log.document_name = invoice_doc.name
		log.from_user = frappe.session.user
		if log.meta.has_field("for_user"):
			log.for_user = recipient.get("for_user") or frappe.session.user
		for fieldname, value in {
			"event_key": event_key,
			"email_to": recipient.get("email"),
			"recipient_email": recipient.get("email"),
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice_doc.name,
		}.items():
			if log.meta.has_field(fieldname):
				setattr(log, fieldname, value)
		if not log.meta.has_field("event_key"):
			log.document_name = event_key
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)
		return log.name


def _notification_event_exists(event_key):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _notification_was_sent(log_name):
	if not log_name or not frappe.db.exists("Notification Log", log_name):
		return False
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["delivery_status", "email_status", "status"]:
		if meta.has_field(fieldname):
			return frappe.db.get_value("Notification Log", log_name, fieldname) == "Sent"
	return False


def _refresh_notification_log(log_name, recipient, subject, message):
	if not log_name or not frappe.db.exists("Notification Log", log_name):
		return
	meta = frappe.get_meta("Notification Log")
	values = {"subject": subject, "email_content": message}
	for fieldname in ["email_to", "recipient_email"]:
		if meta.has_field(fieldname):
			values[fieldname] = recipient.get("email")
	frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _invoice_ineligible_reason(invoice_doc, today):
	if cint(invoice_doc.get("docstatus")) != 1:
		return "Invoice is not submitted."
	if cint(invoice_doc.get("is_return")):
		return "Return invoices do not receive overdue reminders."
	due_date = getdate(invoice_doc.get("due_date")) if invoice_doc.get("due_date") else None
	if not due_date or due_date >= today:
		return "Invoice is not overdue."
	if _invoice_outstanding_amount(invoice_doc) <= 0.005:
		return "Invoice no longer has an outstanding balance."
	return None


def _fail_reserved_reminder(log_name, reason, skipped=False):
	_mark_notification_failed(log_name, reason)
	return {"sent": False, "skipped": bool(skipped), "reason": reason}


def _attempt_datetime(row):
	if isinstance(row, (datetime, date, str)):
		return row
	return row.get("creation") if hasattr(row, "get") else None


def _brisbane_date(value=None):
	if value is None:
		return get_datetime_in_timezone(BRISBANE_TIMEZONE).date()
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return getdate(value)


def _system_datetime_to_brisbane(value):
	datetime_value = get_datetime(value)
	source_timezone = ZoneInfo(get_system_timezone())
	if datetime_value.tzinfo is None:
		datetime_value = datetime_value.replace(tzinfo=source_timezone)
	return datetime_value.astimezone(ZoneInfo(BRISBANE_TIMEZONE)).replace(tzinfo=None)


def _invoice_from_fallback_event_key(event_key):
	text = str(event_key or "")
	if not text.startswith(EVENT_PREFIX):
		return None
	parts = text[len(EVENT_PREFIX) :].rsplit(":", 2)
	return parts[0] if len(parts) == 3 else None


def _ineligible(
	reason_code,
	reason,
	*,
	attempt_count=0,
	last_reminder_at=None,
	days_until_eligible=None,
):
	return {
		"eligible": False,
		"reason_code": reason_code,
		"reason": reason,
		"attempt_count": cint(attempt_count),
		"sequence": None,
		"last_reminder_at": last_reminder_at,
		"days_until_eligible": days_until_eligible,
	}
