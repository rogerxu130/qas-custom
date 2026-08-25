from unittest import TestCase

from qas_custom.services.school_admin_trial_export import (
	_dynamic_form_labels,
	_form_answers,
	_is_export_eligible,
	_invoice_is_paid,
	_safe_export_value,
)


class TestSchoolAdminTrialExport(TestCase):
	def test_paid_invoice_requires_submitted_zero_balance(self):
		self.assertTrue(_invoice_is_paid({"docstatus": 1, "status": "Paid", "outstanding_amount": 10}))
		self.assertTrue(_invoice_is_paid({"docstatus": 1, "status": "Submitted", "outstanding_amount": 0}))
		self.assertFalse(_invoice_is_paid({"docstatus": 0, "status": "Paid", "outstanding_amount": 0}))
		self.assertFalse(_invoice_is_paid({"docstatus": 1, "status": "Cancelled", "outstanding_amount": 0}))

	def test_export_requires_paid_invoice_or_conversion(self):
		self.assertTrue(_is_export_eligible(invoice_paid=True, converted=False))
		self.assertTrue(_is_export_eligible(invoice_paid=False, converted=True))
		self.assertTrue(_is_export_eligible(invoice_paid=True, converted=True))
		self.assertFalse(_is_export_eligible(invoice_paid=False, converted=False))

	def test_form_answers_hide_metadata_and_keep_dynamic_values(self):
		answers = _form_answers(
			{
				"submitted_at": "2026-08-01 10:00:00",
				"referral_source": "Friend",
				"nested": {"special_needs": "Quiet room", "csrf_token": "hidden"},
			}
		)
		self.assertNotIn("submitted at", answers)
		self.assertEqual(answers["referral source"], "Friend")
		self.assertEqual(answers["nested · special needs"], "Quiet room")
		self.assertNotIn("nested · csrf token", answers)

	def test_dynamic_columns_keep_first_appearance_order(self):
		labels = _dynamic_form_labels(
			[
				{"form_answers": {"first": "1", "second": "2"}},
				{"form_answers": {"second": "3", "third": "4"}},
			]
		)
		self.assertEqual(labels, ["first", "second", "third"])

	def test_formula_like_text_is_prefixed(self):
		self.assertEqual(_safe_export_value("=HYPERLINK(\"https://example.com\")"), "'=HYPERLINK(\"https://example.com\")")
		self.assertEqual(_safe_export_value("normal"), "normal")
