from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin import _attach_trial_payment_status


class TestSchoolAdminTrialPaymentStatus(TestCase):
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_uses_only_the_trial_inquiry_invoice_for_mobile_status(self, get_all):
		items = [
			{"id": "INQ-NONE", "inquiry_type": "Trial Lesson", "trial_invoice": ""},
			{"id": "INQ-DRAFT", "inquiry_type": "Trial Lesson", "trial_invoice": "INV-DRAFT"},
			{"id": "INQ-DUE", "inquiry_type": "Trial Lesson", "trial_invoice": "INV-DUE"},
			{"id": "INQ-PAID", "inquiry_type": "Trial Lesson", "trial_invoice": "INV-PAID"},
			{"id": "INQ-FALLBACK", "inquiry_type": "Trial Lesson", "trial_invoice": ""},
			{"id": "VISIT-1", "inquiry_type": "School Visit", "trial_invoice": "INV-PAID"},
		]

		def get_all_rows(doctype, **kwargs):
			fields = kwargs.get("fields") or []
			if doctype == "Sales Invoice" and "source_document" in fields:
				return [{"name": "INV-FALLBACK", "source_document": "INQ-FALLBACK"}]
			if doctype == "Sales Invoice":
				return [
					{"name": "INV-DRAFT", "docstatus": 0, "status": "Draft", "outstanding_amount": 68},
					{"name": "INV-DUE", "docstatus": 1, "status": "Unpaid", "outstanding_amount": 68},
					{"name": "INV-PAID", "docstatus": 1, "status": "Paid", "outstanding_amount": 0},
					{"name": "INV-FALLBACK", "docstatus": 1, "status": "Paid", "outstanding_amount": 0},
					{"name": "UNRELATED-FAMILY-INVOICE", "docstatus": 1, "status": "Unpaid", "outstanding_amount": 500},
				]
			return []

		get_all.side_effect = get_all_rows
		result = _attach_trial_payment_status(items)
		by_id = {item["id"]: item for item in result}

		self.assertEqual(by_id["INQ-NONE"]["trial_payment_status"], "needs_front_desk")
		self.assertEqual(by_id["INQ-DRAFT"]["trial_payment_status"], "needs_front_desk")
		self.assertEqual(by_id["INQ-DUE"]["trial_payment_status"], "needs_front_desk")
		self.assertEqual(by_id["INQ-PAID"]["trial_payment_status"], "paid")
		self.assertEqual(by_id["INQ-FALLBACK"]["trial_invoice"], "INV-FALLBACK")
		self.assertEqual(by_id["INQ-FALLBACK"]["trial_payment_status"], "paid")
		self.assertNotIn("trial_payment_status", by_id["VISIT-1"])

		invoice_call = next(
			call_args
			for call_args in get_all.call_args_list
			if call_args.args[0] == "Sales Invoice" and "docstatus" in (call_args.kwargs.get("fields") or [])
		)
		self.assertNotIn("UNRELATED-FAMILY-INVOICE", invoice_call.kwargs["filters"]["name"][1])
