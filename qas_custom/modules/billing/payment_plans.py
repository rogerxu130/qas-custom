from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime_in_timezone, getdate, now_datetime


PLAN_FLAG_FIELD = "qas_has_payment_plan"
PLAN_STATUS_FIELD = "qas_payment_plan_status"
PLAN_ROWS_FIELD = "qas_payment_plan_installments"
PLAN_CREATED_BY_FIELD = "qas_payment_plan_created_by"
PLAN_CREATED_AT_FIELD = "qas_payment_plan_created_at"
ACTIVE_STATUS = "Active"
COMPLETED_STATUS = "Completed"
EPSILON = 0.005
BRISBANE_TIMEZONE = "Australia/Brisbane"


def has_active_payment_plan(invoice_doc) -> bool:
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	if cint(doc.get("docstatus")) != 1 or not cint(doc.get(PLAN_FLAG_FIELD)):
		return False
	doc = _invoice_doc(doc)
	if flt(doc.get("outstanding_amount")) <= EPSILON:
		return False
	return bool(_plan_rows(doc)) and (doc.get(PLAN_STATUS_FIELD) or ACTIVE_STATUS) == ACTIVE_STATUS


def payment_plan_payload(invoice_doc, today=None):
	doc = _invoice_doc(invoice_doc)
	today = _brisbane_date(today)
	total = flt(doc.get("grand_total"))
	outstanding = max(flt(doc.get("outstanding_amount")), 0)
	total_paid = max(total - outstanding, 0)
	rows = []
	for index, row in enumerate(_plan_rows(doc), start=1):
		target = flt(row.get("cumulative_amount_due"))
		shortfall = max(target - total_paid, 0)
		due_date = getdate(row.get("due_date")) if row.get("due_date") else None
		rows.append(
			{
				"sequence": index,
				"due_date": due_date.isoformat() if due_date else None,
				"cumulative_amount_due": target,
				"total_paid": total_paid,
				"shortfall": shortfall,
				"is_due": bool(due_date and due_date <= today and shortfall > EPSILON),
			}
		)
	active = has_active_payment_plan(doc)
	current = next((row for row in rows if row["is_due"]), None)
	if not current:
		current = next((row for row in rows if row["shortfall"] > EPSILON), None)
	return {
		"enabled": active,
		"status": COMPLETED_STATUS if outstanding <= EPSILON and rows else (doc.get(PLAN_STATUS_FIELD) or (ACTIVE_STATUS if rows else "")),
		"total": total,
		"total_paid": total_paid,
		"outstanding": outstanding,
		"installments": rows,
		"current_installment": current,
	}


def validate_payment_plan(invoice_doc, installments):
	doc = _invoice_doc(invoice_doc)
	if cint(doc.docstatus) != 1:
		frappe.throw(_("A payment plan can only be added after the invoice is submitted."))
	if flt(doc.get("outstanding_amount")) <= EPSILON:
		frappe.throw(_("A payment plan cannot be added to a paid invoice."))
	if _has_payment_or_store_credit(doc):
		frappe.throw(_("A payment plan can only be added before any payment or store credit is applied."))
	rows = _normalise_installments(installments)
	if len(rows) not in (2, 3):
		frappe.throw(_("Choose either 2 or 3 payment installments."))
	previous_date = None
	previous_amount = 0
	for row in rows:
		if previous_date and row["due_date"] <= previous_date:
			frappe.throw(_("Each installment due date must be later than the previous one."))
		if row["cumulative_amount_due"] <= previous_amount + EPSILON:
			frappe.throw(_("Each cumulative amount must be greater than the previous one."))
		previous_date = row["due_date"]
		previous_amount = row["cumulative_amount_due"]
	if abs(rows[-1]["cumulative_amount_due"] - flt(doc.get("grand_total"))) > EPSILON:
		frappe.throw(_("The final cumulative amount must equal the invoice total."))
	last_session_date = _last_linked_enrollment_session_date(doc)
	if last_session_date and rows[-1]["due_date"] > last_session_date:
		frappe.throw(_("The final payment-plan due date cannot be after the last scheduled class ({0}).").format(last_session_date))
	return rows


def apply_payment_plan(invoice_doc, installments, actor=None):
	doc = _invoice_doc(invoice_doc)
	rows = validate_payment_plan(doc, installments)
	doc.set(PLAN_ROWS_FIELD, [])
	for row in rows:
		doc.append(PLAN_ROWS_FIELD, row)
	doc.set(PLAN_FLAG_FIELD, 1)
	doc.set(PLAN_STATUS_FIELD, ACTIVE_STATUS)
	doc.set(PLAN_CREATED_BY_FIELD, actor or frappe.session.user)
	doc.set(PLAN_CREATED_AT_FIELD, now_datetime())
	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
	return doc


def _plan_rows(doc):
	return list(doc.get(PLAN_ROWS_FIELD) or [])


def _invoice_doc(invoice_doc):
	if isinstance(invoice_doc, str):
		return frappe.get_doc("Sales Invoice", invoice_doc)
	# Reminder eligibility receives database rows rather than full documents. Load
	# the submitted invoice so the child-table plan rows are available.
	if isinstance(invoice_doc, dict) and invoice_doc.get("name") and not invoice_doc.get(PLAN_ROWS_FIELD):
		return frappe.get_doc("Sales Invoice", invoice_doc["name"])
	return invoice_doc


def _brisbane_date(value=None):
	return getdate(value) if value else get_datetime_in_timezone(BRISBANE_TIMEZONE).date()


def _normalise_installments(installments):
	if not isinstance(installments, list):
		frappe.throw(_("Payment plan installments are required."))
	rows = []
	for row in installments:
		due_date = getdate((row or {}).get("due_date")) if (row or {}).get("due_date") else None
		amount = flt((row or {}).get("cumulative_amount_due"))
		if not due_date or amount <= 0:
			frappe.throw(_("Every installment needs a due date and cumulative amount."))
		rows.append({"due_date": due_date, "cumulative_amount_due": amount})
	return rows


def _has_payment_or_store_credit(doc):
	from qas_custom.modules.billing.store_credit import get_invoice_store_credit_applied

	if flt(get_invoice_store_credit_applied(doc.name)) > EPSILON:
		return True
	return flt(doc.get("grand_total")) - flt(doc.get("outstanding_amount")) > EPSILON


def _last_linked_enrollment_session_date(doc):
	enrollment = doc.get("enrollment")
	if not enrollment or not frappe.db.exists("Enrollment", enrollment):
		return None
	rows = frappe.get_all(
		"Course Sessions",
		filters={"term": frappe.db.get_value("Enrollment", enrollment, "term"), "weekly_timeslot": frappe.db.get_value("Enrollment", enrollment, "weekly_timeslot"), "status": ["!=", "Cancelled"]},
		pluck="session_date",
		order_by="session_date desc",
		limit_page_length=1,
	)
	return getdate(rows[0]) if rows else None
