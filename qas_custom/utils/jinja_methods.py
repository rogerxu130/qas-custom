from __future__ import annotations

import frappe
from frappe.utils import flt

from qas_custom.modules.billing.store_credit import (
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
	sync_invoice_store_credit_snapshot,
)


def qas_invoice_print_amounts(invoice: str | None = None):
	if not invoice:
		return {"total": 0, "store_credit_applied": 0, "payable_amount": 0}

	doc = frappe.get_doc("Sales Invoice", invoice)
	sync_invoice_store_credit_snapshot(doc)
	doc = frappe.get_doc("Sales Invoice", invoice)
	total = flt(doc.get("grand_total") or doc.get("rounded_total") or 0)
	store_credit_applied = flt(get_invoice_store_credit_applied(invoice))
	payable_amount = flt(get_invoice_payable_amount(doc))
	return {
		"total": total,
		"store_credit_applied": store_credit_applied,
		"payable_amount": payable_amount,
	}
