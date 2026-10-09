from __future__ import annotations

from qas_custom.modules.billing.presentation import get_invoice_print_context


def qas_invoice_print_amounts(invoice: str | None = None):
	if not invoice:
		return {"total": 0, "store_credit_applied": 0, "payable_amount": 0}
	return get_invoice_print_context(invoice)
