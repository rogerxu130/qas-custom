from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.billing.drafts import new_invoice_draft


MODULE = "qas_custom.modules.billing.drafts"


class _Meta:
	def __init__(self, fields):
		self.fields = fields

	def has_field(self, fieldname):
		return fieldname in self.fields


class _Invoice(frappe._dict):
	def __init__(self, fields):
		super().__init__(payment_schedule=[])
		self.meta = _Meta(fields)
		self.save = Mock()
		self.insert = Mock()
		self.submit = Mock()

	def set(self, fieldname, value):
		self[fieldname] = value


class TestInvoiceDraftFactory(TestCase):
	def _frappe_stub(self, invoice):
		return SimpleNamespace(new_doc=Mock(return_value=invoice), db=SimpleNamespace(commit=Mock()))

	def test_initializes_customer_dates_and_available_custom_fields_without_saving(self):
		invoice = _Invoice({"parent", "qas_invoice_type"})
		frappe_stub = self._frappe_stub(invoice)
		with patch(MODULE + ".frappe", frappe_stub), patch(
			"qas_custom.modules.billing.invoice_settings.nowdate", return_value="2026-09-23"
		), patch(
			"qas_custom.modules.billing.invoice_settings.get_default_invoice_due_date",
			return_value="2026-09-30",
		) as default_due_date:
			result = new_invoice_draft(customer="CUST-001", parent="PARENT-001", invoice_type="Workshop")

		self.assertIs(result, invoice)
		frappe_stub.new_doc.assert_called_once_with("Sales Invoice")
		default_due_date.assert_called_once_with("2026-09-23")
		self.assertEqual(invoice.customer, "CUST-001")
		self.assertEqual(invoice.posting_date, "2026-09-23")
		self.assertEqual(invoice.due_date, "2026-09-30")
		self.assertEqual(invoice.parent, "PARENT-001")
		self.assertEqual(invoice.qas_invoice_type, "Workshop")
		self._assert_no_persistence(invoice, frappe_stub.db.commit)

	def test_missing_custom_fields_are_ignored(self):
		invoice = _Invoice(set())
		frappe_stub = self._frappe_stub(invoice)
		with patch(MODULE + ".frappe", frappe_stub), patch(
			MODULE + ".apply_default_invoice_dates"
		):
			result = new_invoice_draft(customer="CUST-001", parent="PARENT-001", invoice_type="Course")

		self.assertIs(result, invoice)
		self.assertEqual(invoice.customer, "CUST-001")
		self.assertNotIn("parent", invoice)
		self.assertNotIn("qas_invoice_type", invoice)
		self._assert_no_persistence(invoice, frappe_stub.db.commit)

	def test_date_initialization_failure_propagates_without_saving(self):
		invoice = _Invoice({"parent", "qas_invoice_type"})
		frappe_stub = self._frappe_stub(invoice)
		with patch(MODULE + ".frappe", frappe_stub), patch(
			MODULE + ".apply_default_invoice_dates", side_effect=ValueError("invalid invoice date")
		):
			with self.assertRaisesRegex(ValueError, "invalid invoice date"):
				new_invoice_draft(customer="CUST-001", parent="PARENT-001", invoice_type="Course")

		self._assert_no_persistence(invoice, frappe_stub.db.commit)

	def _assert_no_persistence(self, invoice, commit):
		invoice.save.assert_not_called()
		invoice.insert.assert_not_called()
		invoice.submit.assert_not_called()
		commit.assert_not_called()
