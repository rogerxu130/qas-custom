from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate, now_datetime

from qas_custom.modules.notifications.commands import _invoice_recipient
from qas_custom.modules.notifications.invoice_overdue_reminders import (
	_brisbane_date,
	_get_reminder_history,
	overdue_reminder_eligibility,
	queue_overdue_invoice_reminder,
)


ADMIN_ROLES = {"School Admin", "System Manager"}
TERM_REMINDER_JOB_TTL_SECONDS = 86400


def get_term_overdue_invoice_reminder_preview_data(term=None):
	_require_school_admin()
	term = _validate_term(term)
	today = _brisbane_date()
	invoice_names = _term_invoice_names(term)
	invoice_rows = _term_invoice_rows(invoice_names)
	item_rows = _term_invoice_item_rows(term, invoice_names)
	history = _get_reminder_history(invoice_names)
	students_by_invoice = _students_by_invoice(item_rows)

	eligible_items = []
	excluded_counts = defaultdict(int)
	total_outstanding = 0.0
	parent_keys = set()

	for invoice in invoice_rows:
		eligibility = overdue_reminder_eligibility(invoice, history.get(invoice.name, []), today)
		recipient = None
		if eligibility.get("eligible"):
			recipient = _invoice_recipient(invoice)
			if not recipient.get("email"):
				eligibility = {
					**eligibility,
					"eligible": False,
					"reason_code": "missing_recipient",
					"reason": _("No parent email found."),
					"sequence": None,
				}
		if not eligibility.get("eligible"):
			excluded_counts[eligibility.get("reason_code") or "ineligible"] += 1
			continue

		outstanding = flt(invoice.get("outstanding_amount"))
		total_outstanding += outstanding
		parent_key = recipient.get("parent") or recipient.get("customer") or recipient.get("email")
		if parent_key:
			parent_keys.add(parent_key)
		eligible_items.append(
			{
				"invoice": invoice.name,
				"customer": invoice.get("customer"),
				"parent": recipient.get("parent"),
				"recipient_email": recipient.get("email"),
				"students": students_by_invoice.get(invoice.name, []),
				"due_date": invoice.get("due_date"),
				"days_overdue": date_diff(today, getdate(invoice.get("due_date"))),
				"outstanding_amount": outstanding,
				"attempt_count": eligibility.get("attempt_count") or 0,
				"next_sequence": eligibility.get("sequence"),
				"last_reminder_at": _serialise_datetime(eligibility.get("last_reminder_at")),
			}
		)

	eligible_items.sort(key=lambda row: (getdate(row.get("due_date")), row.get("invoice")))
	return {
		"term": term,
		"today": today.isoformat(),
		"generated_at": now_datetime().isoformat(),
		"matching_invoice_count": len(invoice_names),
		"eligible_invoice_count": len(eligible_items),
		"unique_parent_count": len(parent_keys),
		"total_outstanding": total_outstanding,
		"excluded_count": sum(excluded_counts.values()),
		"excluded_counts": dict(sorted(excluded_counts.items())),
		"items": eligible_items,
	}


def start_term_overdue_invoice_reminder_job_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	term = _validate_term(payload.get("term"))
	preview = get_term_overdue_invoice_reminder_preview_data(term)
	invoices = [row.get("invoice") for row in preview.get("items", []) if row.get("invoice")]
	if not invoices:
		frappe.throw(_("No overdue invoices are currently eligible for this Term."))

	job_id = frappe.generate_hash(length=16)
	status = _initial_job_status(job_id, term, invoices, preview)
	_set_job_status(job_id, status)
	frappe.enqueue(
		"qas_custom.services.term_overdue_invoice_reminders.run_term_overdue_invoice_reminder_job",
		queue="long",
		timeout=3600,
		job_name="QAS Term overdue reminders {0}".format(job_id),
		enqueue_after_commit=True,
		qas_job_id=job_id,
		term=term,
		invoices=invoices,
		requested_by=frappe.session.user,
	)
	return status


def get_term_overdue_invoice_reminder_job_data(job_id=None):
	_require_school_admin()
	job_id = str(job_id or "").strip()
	if not job_id:
		frappe.throw(_("Job ID is required."))
	status = _get_job_status(job_id)
	if not status:
		frappe.throw(_("Term overdue reminder job was not found or has expired."))
	return status


def run_term_overdue_invoice_reminder_job(qas_job_id=None, term=None, invoices=None, requested_by=None):
	job_id = str(qas_job_id or "").strip()
	term = str(term or "").strip()
	invoice_names = _unique_names(invoices or [])
	if not job_id:
		return
	if requested_by:
		frappe.set_user(requested_by)

	status = _get_job_status(job_id) or _initial_job_status(job_id, term, invoice_names)
	status.update({"status": "running", "started_at": now_datetime().isoformat(), "current_invoice": None})
	_set_job_status(job_id, status)

	for invoice in invoice_names:
		status["current_invoice"] = invoice
		_set_job_status(job_id, status)
		try:
			result = queue_overdue_invoice_reminder(invoice)
			result_row = {
				"invoice": invoice,
				"queued": bool(result.get("queued")),
				"skipped": bool(result.get("skipped")),
				"reason_code": result.get("reason_code"),
				"message": result.get("reason") or (_("Queued for delivery") if result.get("queued") else _("Not queued")),
				"notification_log": result.get("notification_log"),
			}
			status["results"].append(result_row)
			status["processed"] += 1
			if result_row["queued"]:
				status["queued"] += 1
			elif result_row["skipped"]:
				status["skipped"] += 1
			else:
				status["failed"] += 1
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			status["processed"] += 1
			status["failed"] += 1
			status["results"].append(
				{
					"invoice": invoice,
					"queued": False,
					"skipped": False,
					"message": _error_message(exc),
				}
			)
		_set_job_status(job_id, status)

	status["current_invoice"] = None
	status["completed_at"] = now_datetime().isoformat()
	status["status"] = "completed_with_errors" if status.get("failed") else "completed"
	_set_job_status(job_id, status)
	return status


def _term_invoice_names(term):
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"term": term, "parenttype": "Sales Invoice"},
		pluck="parent",
		order_by="parent asc",
		limit_page_length=0,
	)
	return _unique_names(rows)


def _term_invoice_rows(invoice_names):
	if not invoice_names:
		return []
	fields = ["name", "customer", "due_date", "outstanding_amount", "grand_total", "docstatus", "status"]
	for fieldname in ["is_return", "parent", "contact_email", "email", "email_id"]:
		if frappe.db.has_column("Sales Invoice", fieldname):
			fields.append(fieldname)
	return frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", invoice_names]},
		fields=fields,
		order_by="due_date asc, name asc",
		limit_page_length=0,
	)


def _term_invoice_item_rows(term, invoice_names):
	if not invoice_names:
		return []
	fields = ["parent", "student", "term"]
	if frappe.db.has_column("Sales Invoice Item", "student_display_name"):
		fields.append("student_display_name")
	return frappe.get_all(
		"Sales Invoice Item",
		filters={"term": term, "parent": ["in", invoice_names], "parenttype": "Sales Invoice"},
		fields=fields,
		order_by="parent asc, idx asc",
		limit_page_length=0,
	)


def _students_by_invoice(item_rows):
	result = defaultdict(list)
	seen = defaultdict(set)
	for row in item_rows or []:
		invoice = row.get("parent")
		student = row.get("student_display_name") or row.get("student")
		if invoice and student and student not in seen[invoice]:
			seen[invoice].add(student)
			result[invoice].append(student)
	return dict(result)


def _validate_term(term):
	term = str(term or "").strip()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	return term


def _require_school_admin():
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)


def _get_payload(payload):
	if isinstance(payload, str):
		payload = frappe.parse_json(payload)
	return payload or {}


def _unique_names(values):
	names = []
	seen = set()
	for value in values or []:
		name = str(value or "").strip()
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def _initial_job_status(job_id, term, invoices, preview=None):
	return {
		"job_id": job_id,
		"status": "queued",
		"term": term,
		"total": len(invoices or []),
		"processed": 0,
		"queued": 0,
		"skipped": 0,
		"failed": 0,
		"current_invoice": None,
		"results": [],
		"preview": {
			"unique_parent_count": (preview or {}).get("unique_parent_count", 0),
			"total_outstanding": (preview or {}).get("total_outstanding", 0),
		},
		"created_at": now_datetime().isoformat(),
		"started_at": None,
		"completed_at": None,
	}


def _job_cache_key(job_id):
	return "qas:school_admin:term_overdue_invoice_reminders:{0}".format(job_id)


def _set_job_status(job_id, status):
	frappe.cache().set_value(
		_job_cache_key(job_id),
		status,
		expires_in_sec=TERM_REMINDER_JOB_TTL_SECONDS,
	)


def _get_job_status(job_id):
	return frappe.cache().get_value(_job_cache_key(job_id))


def _serialise_datetime(value):
	if not value:
		return None
	return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _error_message(exc):
	if frappe.message_log:
		message = frappe.message_log.pop()
		frappe.message_log.clear()
		if isinstance(message, dict):
			return message.get("message") or message.get("title") or str(exc)
		return str(message)
	return str(exc)
