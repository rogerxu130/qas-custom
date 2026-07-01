from __future__ import annotations

from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts


def qas_invoice_print_amounts(invoice: str | None = None):
	if not invoice:
		return {"total": 0, "store_credit_applied": 0, "payable_amount": 0}
	return resolve_invoice_print_amounts(invoice)
