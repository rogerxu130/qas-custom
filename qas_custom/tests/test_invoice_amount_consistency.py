from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts
from qas_custom.modules.billing.store_credit import (
	enforce_exact_draft_invoice_total,
	get_invoice_payable_amount,
	get_invoice_total_amount,
)


class _Meta:
	def __init__(self, fields=()):
		self.fields = set(fields)

	def get_field(self, fieldname):
		return fieldname if fieldname in self.fields else None


class _Invoice(dict):
	def __init__(self, *args, fields=("disable_rounded_total",), **kwargs):
		super().__init__(*args, **kwargs)
		self.meta = _Meta(fields)

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def set(self, key, value):
		self[key] = value


class InvoiceAmountConsistencyTest(TestCase):
	def test_draft_total_uses_exact_grand_total(self):
		invoice = _Invoice(docstatus=0, grand_total=536.67, rounded_total=537.0)

		self.assertEqual(get_invoice_total_amount(invoice), 536.67)

	def test_legacy_submitted_total_retains_accounting_rounding(self):
		invoice = _Invoice(docstatus=1, grand_total=536.67, rounded_total=537.0)

		self.assertEqual(get_invoice_total_amount(invoice), 537.0)

	@patch("qas_custom.modules.billing.invoice_amounts._invoice_outstanding_amount", return_value=537.0)
	@patch("qas_custom.modules.billing.invoice_amounts._submitted_payment_entry_amount", return_value=0)
	@patch("qas_custom.modules.billing.invoice_amounts.get_invoice_store_credit_applied", return_value=0)
	@patch("qas_custom.modules.billing.invoice_amounts.get_invoice_payable_amount", return_value=537.0)
	def test_print_amounts_use_submitted_accounting_total(self, _payable, _store_credit, _payments, _outstanding):
		invoice = _Invoice(name="ACC-SINV-LEGACY", docstatus=1, grand_total=536.67, rounded_total=537.0)

		amounts = resolve_invoice_print_amounts(invoice, sync_snapshot=False)

		self.assertEqual(amounts["total"], 537.0)
		self.assertEqual(amounts["payable_amount"], 537.0)

	def test_draft_validation_disables_rounded_total(self):
		invoice = _Invoice(docstatus=0, disable_rounded_total=0)

		enforce_exact_draft_invoice_total(invoice)

		self.assertEqual(invoice.disable_rounded_total, 1)

	def test_non_draft_validation_does_not_change_rounding(self):
		for docstatus in (1, 2):
			with self.subTest(docstatus=docstatus):
				invoice = _Invoice(docstatus=docstatus, disable_rounded_total=0)

				enforce_exact_draft_invoice_total(invoice)

				self.assertEqual(invoice.disable_rounded_total, 0)

	@patch("qas_custom.modules.billing.store_credit.get_invoice_store_credit_applied", return_value=0)
	@patch("qas_custom.modules.billing.store_credit._invoice_outstanding_amount", return_value=537.0)
	def test_draft_payable_uses_exact_total(self, _outstanding, _store_credit):
		invoice = _Invoice(name="ACC-SINV-TEST", docstatus=0, grand_total=536.67, rounded_total=537.0)

		self.assertEqual(get_invoice_payable_amount(invoice), 536.67)
