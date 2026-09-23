"""Invoice portal links use persisted invoice provenance, without a site."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.billing import presentation


class TestParentInvoicePaygLink(TestCase):
	def setUp(self):
		self.get_value = Mock()
		for target, value in (("db", SimpleNamespace(get_value=self.get_value)),
		                      ("conf", {"qas_parent_portal_url": "https://portal.example.com/"})):
			setting = patch.object(presentation.frappe, target, value)
			setting.start()
			self.addCleanup(setting.stop)

	def test_payg_invoice_types_use_psugo(self):
		for invoice_type in ("PAYG Card", "PAYG Exchange"):
			with self.subTest(invoice_type=invoice_type):
				self.get_value.return_value = invoice_type
				self.assertEqual(presentation.parent_portal_invoice_link("SINV-1"),
				                 "https://portal.example.com/psugo?section=invoices&invoice=SINV-1")
				self.get_value.assert_called_with("Sales Invoice", "SINV-1", "qas_invoice_type")

	def test_ordinary_and_missing_invoices_stay_on_parent_portal(self):
		for invoice_type in ("Course", "Other", "Workshop", None):
			with self.subTest(invoice_type=invoice_type):
				self.get_value.return_value = invoice_type
				self.assertEqual(presentation.parent_portal_invoice_link("SINV-1"),
				                 "https://portal.example.com/invoices?invoice=SINV-1")

	def test_db_failure_falls_back_to_parent_portal(self):
		self.get_value.side_effect = RuntimeError("database unavailable")
		self.assertEqual(presentation.parent_portal_invoice_link("SINV-1"),
		                 "https://portal.example.com/invoices?invoice=SINV-1")

	def test_invoice_identifier_is_url_encoded(self):
		self.get_value.return_value = "PAYG Card"
		self.assertEqual(presentation.parent_portal_invoice_link("SINV A&B/1"),
		                 "https://portal.example.com/psugo?section=invoices&invoice=SINV+A%26B%2F1")
