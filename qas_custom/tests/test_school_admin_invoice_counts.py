from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import school_admin


class TestSchoolAdminInvoiceCounts(TestCase):
	def test_invoice_counts_use_the_list_query_for_nullable_source_fields(self):
		# db.count's raw != drops NULL sources; get_all normalizes them for the list.
		fake = SimpleNamespace(get_all=Mock(return_value=[frappe._dict(total=1)]),
			db=SimpleNamespace(count=Mock(return_value=0)))
		with patch.object(school_admin, "frappe", fake), patch.object(
			school_admin, "_doctype_available", return_value=True
		):
			for status in (0, 1, 2):
				filters = {"docstatus": status}
				self.assertEqual(school_admin._count_sales_invoices(filters), 1)
				self.assertEqual(filters, {"docstatus": status})
				fake.get_all.assert_called_with("Sales Invoice", filters={
					"docstatus": status,
					"qas_invoice_type": ["not in", ["Store Order", "Material Order"]],
					"source_doctype": ["!=", "Store Order"],
				}, fields=["count(name) as total"])
		fake.db.count.assert_not_called()

	def test_no_matching_invoices_returns_zero(self):
		with patch.object(school_admin, "_doctype_available", return_value=True), patch.object(
			school_admin.frappe, "get_all", return_value=[frappe._dict(total=0)]
		):
			self.assertEqual(school_admin._count_sales_invoices({"docstatus": 0}), 0)

	def test_unavailable_table_or_failed_query_retains_dashboard_fallback(self):
		with patch.object(school_admin, "_doctype_available", return_value=False), patch.object(
			school_admin.frappe, "get_all"
		) as get_all:
			self.assertEqual(school_admin._count_sales_invoices({}), 0)
			get_all.assert_not_called()
		with patch.object(school_admin, "_doctype_available", return_value=True), patch.object(
			school_admin.frappe, "get_all", side_effect=RuntimeError("query failed")
		):
			self.assertEqual(school_admin._count_sales_invoices({}), 0)
