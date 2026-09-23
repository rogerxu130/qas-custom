"""Initialize unsaved invoice drafts without product-specific side effects."""

from __future__ import annotations

import frappe

from qas_custom.modules.billing.invoice_settings import apply_default_invoice_dates
from qas_custom.modules.common import set_if_field


def new_invoice_draft(*, customer: str, parent: str | None, invoice_type: str):
	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	apply_default_invoice_dates(invoice)
	set_if_field(invoice, "parent", parent)
	set_if_field(invoice, "qas_invoice_type", invoice_type)
	return invoice
