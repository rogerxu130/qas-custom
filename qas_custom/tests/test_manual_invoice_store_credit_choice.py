from unittest import TestCase

import frappe

from qas_custom.modules.billing.store_credit import _store_credit_application_eligibility


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
