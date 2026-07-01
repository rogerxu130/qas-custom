from __future__ import annotations

import frappe
from frappe.utils import flt

from qas_custom.modules.billing.store_credit import (
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
)


def qas_invoice_print_amounts(invoice: str | None = None):
	if not invoice:
		return {"total": 0, "store_credit_applied": 0, "payable_amount": 0}

	doc = frappe.get_doc("Sales Invoice", invoice)
	total = flt(doc.get("grand_total") or doc.get("rounded_total") or 0)
	snapshot_credit = flt(doc.get("qas_store_credit_applied") or 0)
	snapshot_payable = flt(doc.get("qas_amount_payable") or 0)
	ledger_credit = flt(get_invoice_store_credit_applied(invoice))
	calculated_payable = flt(get_invoice_payable_amount(doc))

	store_credit_applied = max(ledger_credit, snapshot_credit)
	if store_credit_applied > 0:
		payable_amount = max(0, total - store_credit_applied)
		if doc.get("docstatus") == 1:
			payable_amount = min(payable_amount, calculated_payable)
		if snapshot_payable > 0:
			payable_amount = min(payable_amount, snapshot_payable)
	else:
		payable_amount = calculated_payable

	_sync_print_amount_snapshot(invoice, store_credit_applied, payable_amount)
	return {
		"total": total,
		"store_credit_applied": store_credit_applied,
		"payable_amount": payable_amount,
	}


def _sync_print_amount_snapshot(invoice: str, store_credit_applied: float, payable_amount: float):
	updates = {}
	if frappe.db.has_column("Sales Invoice", "qas_store_credit_applied"):
		updates["qas_store_credit_applied"] = flt(store_credit_applied)
	if frappe.db.has_column("Sales Invoice", "qas_amount_payable"):
		updates["qas_amount_payable"] = flt(payable_amount)
	if updates:
		frappe.db.set_value("Sales Invoice", invoice, updates, update_modified=False)
