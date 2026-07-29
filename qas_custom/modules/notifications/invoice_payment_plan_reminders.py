from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, get_datetime_in_timezone, getdate

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.payment_plans import has_active_payment_plan, payment_plan_payload
from qas_custom.modules.billing.presentation import parent_portal_invoice_link
from qas_custom.modules.notifications.commands import _invoice_pdf_attachment, _invoice_recipient
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


EVENT_PREFIX = "invoice_payment_plan_reminder:"
REMINDER_INTERVAL_DAYS = 3
MAX_REMINDER_ATTEMPTS = 5
BRISBANE_TIMEZONE = "Australia/Brisbane"


def run_payment_plan_reminders(today=None):
	if not outbound_email_enabled():
		return {"skipped": True, "reason": email_block_reason()}
	today = _brisbane_date(today)
	result = {"eligible": 0, "queued": 0, "skipped": 0, "failed": 0}
	for row in frappe.get_all("Sales Invoice", filters={"docstatus": 1, "qas_has_payment_plan": 1, "outstanding_amount": [">", 0.005]}, pluck="name", limit_page_length=0):
		try:
			doc = frappe.get_doc("Sales Invoice", row)
			if not has_active_payment_plan(doc):
				continue
			for installment in payment_plan_payload(doc, today=today).get("installments", []):
				if not installment.get("is_due"):
					continue
				result["eligible"] += 1
				key = _event_key(doc.name, installment["sequence"], today)
				if _recent_or_max_attempt(key, today):
					result["skipped"] += 1
					continue
				frappe.enqueue(
					"qas_custom.modules.notifications.invoice_payment_plan_reminders.send_payment_plan_reminder_job",
					queue="short", timeout=300, enqueue_after_commit=True, deduplicate=True,
					job_id=key.replace(":", "-"), invoice=doc.name, installment_sequence=installment["sequence"], event_key=key,
				)
				result["queued"] += 1
		except Exception:
			result["failed"] += 1
			frappe.log_error(frappe.get_traceback(), "QAS payment plan reminder queue failed")
	return result


def send_payment_plan_reminder_job(invoice, installment_sequence, event_key, today=None):
	today = _brisbane_date(today)
	doc = frappe.get_doc("Sales Invoice", invoice)
	plan = payment_plan_payload(doc, today=today)
	if not has_active_payment_plan(doc):
		return {"sent": False, "skipped": True, "reason": "Payment plan is not active."}
	installment = next((row for row in plan.get("installments", []) if cint(row.get("sequence")) == cint(installment_sequence)), None)
	if not installment or not installment.get("is_due"):
		return {"sent": False, "skipped": True, "reason": "Installment is no longer due."}
	recipient = _invoice_recipient(doc)
	if not recipient.get("email"):
		return {"sent": False, "skipped": True, "reason": "No parent email found."}
	subject = _("Payment plan reminder: Invoice {0}").format(doc.name)
	message = _message(doc, plan, installment)
	try:
		mail_result = sendmail_or_skip(
			action="parent_invoice_payment_plan_reminder", recipients=[recipient["email"]], subject=subject, message=message,
			reference_doctype="Sales Invoice", reference_name=doc.name,
			reply_to=get_invoice_settings().get("school_email") or None, delayed=False,
			attachments=[_invoice_pdf_attachment(doc.name, payable_amount=flt(doc.outstanding_amount))],
		)
		if mail_result and mail_result.get("skipped"):
			return {"sent": False, "skipped": True, "reason": mail_result.get("reason")}
		_record_attempt(event_key, doc.name, installment_sequence)
		return {"sent": True, "invoice": doc.name, "installment": installment_sequence, "recipient": recipient["email"]}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS payment plan reminder failed: {0}".format(doc.name))
		raise


def _message(doc, plan, current):
	rows = []
	for row in plan.get("installments", []):
		rows.append(
			"<tr><td style='padding:8px;border-bottom:1px solid #e2e8f0'>{0}</td><td style='padding:8px;border-bottom:1px solid #e2e8f0'>{1}</td></tr>".format(
				escape_html(getdate(row["due_date"]).strftime("%d %b %Y")),
				escape_html("${:,.2f}".format(flt(row["cumulative_amount_due"]))),
			)
		)
	return """<div style='font-family:Arial,sans-serif;color:#172033;line-height:1.55'>
<h2>Payment plan reminder</h2><p>Your payment-plan installment for invoice <strong>{invoice}</strong> is now due.</p>
<p><strong>Current installment shortfall: ${shortfall:,.2f}</strong><br>Invoice total: ${total:,.2f}<br>Paid so far: ${paid:,.2f}<br>Outstanding: ${outstanding:,.2f}</p>
<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;width:100%'><thead><tr><th align='left'>Due date</th><th align='left'>Cumulative amount due</th></tr></thead><tbody>{rows}</tbody></table>
<p style='margin-top:20px'><a href='{link}' style='background:#1a2b4a;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none'>View invoice in Parent Portal</a></p></div>""".format(
		invoice=escape_html(doc.name), shortfall=flt(current["shortfall"]), total=flt(plan["total"]), paid=flt(plan["total_paid"]), outstanding=flt(plan["outstanding"]), rows="".join(rows), link=parent_portal_invoice_link(doc.name)
	)


def _brisbane_date(value=None):
	return getdate(value) if value else get_datetime_in_timezone(BRISBANE_TIMEZONE).date()


def _event_key(invoice, sequence, today):
	return "{0}{1}:{2}:{3}".format(EVENT_PREFIX, invoice, cint(sequence), getdate(today).isoformat())


def _recent_or_max_attempt(event_key, today):
	prefix = ":".join(event_key.split(":")[:-1]) + ":"
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": ["like", prefix + "%"]} if meta.has_field("event_key") else {"document_name": ["like", prefix + "%"]}
	if meta.has_field("event_key"):
		filters["document_type"] = "Sales Invoice"
	rows = frappe.get_all("Notification Log", filters=filters, fields=["creation"], order_by="creation desc", limit_page_length=MAX_REMINDER_ATTEMPTS + 1)
	if len(rows) >= MAX_REMINDER_ATTEMPTS:
		return True
	if not rows:
		return False
	return (getdate(today) - getdate(rows[0].get("creation"))).days < REMINDER_INTERVAL_DAYS


def _record_attempt(event_key, invoice, sequence):
	log = frappe.new_doc("Notification Log")
	log.update({"subject": "Payment plan reminder", "type": "Alert", "document_type": "Sales Invoice", "document_name": invoice})
	if log.meta.has_field("event_key"):
		log.event_key = event_key
	else:
		log.document_name = event_key
	if log.meta.has_field("reference_doctype"):
		log.reference_doctype = "Sales Invoice"
	if log.meta.has_field("reference_name"):
		log.reference_name = invoice
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
