from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from qas_custom.modules.billing.store_credit import (
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
)


def resolve_invoice_print_amounts(
	invoice_doc,
	*,
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
	sync_snapshot: bool = True,
):
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	total = flt(doc.get("grand_total") or doc.get("rounded_total") or 0)
	outstanding = _invoice_outstanding_amount(doc, total)
	ledger_credit = flt(get_invoice_store_credit_applied(doc.name))
	snapshot_credit = flt(doc.get("qas_store_credit_applied") or 0)
	passed_credit = flt(store_credit_applied) if store_credit_applied is not None else 0
	outstanding_credit = _invoice_credit_from_outstanding(doc, total, outstanding)
	credit = _clamp_amount(max(ledger_credit, snapshot_credit, passed_credit, outstanding_credit), total)

	if credit > 0:
		payable = max(0, total - credit)
		calculated_payable = flt(get_invoice_payable_amount(doc))
		snapshot_payable = flt(doc.get("qas_amount_payable") or 0)
		if payable_amount is not None and flt(payable_amount) > 0:
			payable = min(payable, flt(payable_amount))
		if calculated_payable > 0:
			payable = min(payable, calculated_payable)
		if snapshot_payable > 0:
			payable = min(payable, snapshot_payable)
		if outstanding >= 0:
			payable = min(payable, outstanding)
	elif payable_amount is not None:
		payable = flt(payable_amount)
	else:
		payable = flt(get_invoice_payable_amount(doc))

	payable = max(0, payable)
	if sync_snapshot:
		sync_invoice_print_amount_snapshot(doc.name, store_credit_applied=credit, payable_amount=payable)
	return {
		"total": total,
		"store_credit_applied": credit,
		"payable_amount": payable,
	}


def sync_invoice_print_amount_snapshot(invoice: str, *, store_credit_applied=None, payable_amount=None):
	if not invoice:
		return {}
	doc = frappe.get_doc("Sales Invoice", invoice)
	updates = {}
	if store_credit_applied is None:
		store_credit_applied = resolve_invoice_print_amounts(doc, sync_snapshot=False)["store_credit_applied"]
	if payable_amount is None:
		payable_amount = resolve_invoice_print_amounts(
			doc,
			store_credit_applied=store_credit_applied,
			sync_snapshot=False,
		)["payable_amount"]
	if frappe.db.has_column("Sales Invoice", "qas_store_credit_applied"):
		updates["qas_store_credit_applied"] = flt(store_credit_applied)
	if frappe.db.has_column("Sales Invoice", "qas_amount_payable"):
		updates["qas_amount_payable"] = flt(payable_amount)
	if updates:
		frappe.db.set_value("Sales Invoice", invoice, updates, update_modified=False)
	return updates


def _invoice_credit_from_outstanding(doc, total: float, outstanding: float) -> float:
	if cint(doc.get("docstatus") or 0) != 1 or total <= 0:
		return 0
	if outstanding < 0:
		outstanding = 0
	payment_entry_amount = _submitted_payment_entry_amount(doc.name)
	return max(0, total - outstanding - payment_entry_amount)


def _submitted_payment_entry_amount(invoice: str) -> float:
	if not invoice or not _doctype_available("Payment Entry Reference") or not _doctype_available("Payment Entry"):
		return 0
	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
		fields=["parent", "allocated_amount"],
		limit_page_length=0,
	)
	if not rows:
		return 0
	payment_entries = sorted({row.get("parent") for row in rows if row.get("parent")})
	if not payment_entries:
		return 0
	submitted = set(
		frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", payment_entries], "docstatus": 1},
			pluck="name",
			limit_page_length=0,
		)
	)
	return sum(flt(row.get("allocated_amount") or 0) for row in rows if row.get("parent") in submitted)


def _invoice_outstanding_amount(doc, total: float) -> float:
	invoice = doc.get("name")
	if invoice and frappe.db.has_column("Sales Invoice", "outstanding_amount"):
		value = frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")
		if value is not None:
			return flt(value)
	value = doc.get("outstanding_amount")
	if value is not None:
		return flt(value)
	return total


def _clamp_amount(value: float, ceiling: float) -> float:
	if ceiling <= 0:
		return max(0, flt(value))
	return min(max(0, flt(value)), flt(ceiling))


def _doctype_available(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))
