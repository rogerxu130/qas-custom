from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.billing.store_credit import _store_credit_application_eligibility
from qas_custom.patches.v2026_07_20_add_manual_invoice_marker import _backfill_manual_invoices


class TestManualInvoiceStoreCreditChoice(TestCase):
	def test_manual_other_invoice_can_explicitly_apply_store_credit(self):
		invoice = frappe._dict(
			qas_is_manual_invoice=1,
			qas_invoice_type="Other",
			qas_apply_store_credit_on_submit=1,
			items=[],
		)

		self.assertEqual(_store_credit_application_eligibility(invoice), (True, None))

	def test_manual_course_invoice_can_explicitly_skip_store_credit(self):
		invoice = frappe._dict(
			source_type="Manual",
			qas_invoice_type="Course",
			qas_apply_store_credit_on_submit=0,
			items=[],
		)

		eligible, reason = _store_credit_application_eligibility(invoice)
		self.assertFalse(eligible)
		self.assertIn("disabled", reason)

	def test_existing_non_manual_invoice_rules_are_unchanged(self):
		course_invoice = frappe._dict(source_type="Enrollment", qas_invoice_type="Course", items=[])
		other_invoice = frappe._dict(source_type="Enrollment", qas_invoice_type="Other", items=[])

		self.assertEqual(_store_credit_application_eligibility(course_invoice), (True, None))
		self.assertEqual(
			_store_credit_application_eligibility(other_invoice),
			(False, "Invoice is not course-related."),
		)

	def test_manual_invoice_backfill_only_uses_production_columns(self):
		db = Mock()
		db.has_column.return_value = True
		with patch(
			"qas_custom.patches.v2026_07_20_add_manual_invoice_marker.frappe",
			SimpleNamespace(db=db),
		):
			_backfill_manual_invoices()

		query = db.sql.call_args.args[0]
		self.assertIn("qas_is_manual_invoice", query)
		self.assertNotIn("enrollment", query)
