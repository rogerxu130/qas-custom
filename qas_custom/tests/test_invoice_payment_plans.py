from datetime import date, datetime
from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.modules.billing.payment_plans import payment_plan_payload
from qas_custom.modules.notifications.invoice_overdue_reminders import overdue_reminder_eligibility
from qas_custom.modules.notifications.invoice_payment_plan_reminders import _recent_or_max_attempt
from qas_custom.modules.notifications.commands import _invoice_pdf_payment_plan_block


def invoice(**overrides):
	values = {
		"name": "ACC-SINV-2026-00001",
		"docstatus": 1,
		"is_return": 0,
		"due_date": "2026-07-01",
		"grand_total": 600,
		"outstanding_amount": 400,
		"qas_has_payment_plan": 1,
		"qas_payment_plan_status": "Active",
		"qas_payment_plan_installments": [
			frappe._dict({"due_date": "2026-07-10", "cumulative_amount_due": 200}),
			frappe._dict({"due_date": "2026-07-20", "cumulative_amount_due": 600}),
		],
	}
	values.update(overrides)
	return frappe._dict(values)


class TestInvoicePaymentPlans(TestCase):
	def test_invoice_pdf_includes_active_payment_plan_schedule(self):
		plan = payment_plan_payload(invoice(), today=date(2026, 7, 12))
		html = _invoice_pdf_payment_plan_block({"payment_plan": plan})

		self.assertIn("Payment plan", html)
		self.assertIn("Installment 1", html)
		self.assertIn("Installment 2", html)
		self.assertIn("AUD $200.00", html)
		self.assertIn("AUD $600.00", html)

	def test_invoice_pdf_hides_payment_plan_when_not_active(self):
		self.assertEqual(_invoice_pdf_payment_plan_block({"payment_plan": {"enabled": False}}), "")

	def test_payload_uses_cumulative_target_and_amount_paid(self):
		plan = payment_plan_payload(invoice(), today=date(2026, 7, 12))

		self.assertTrue(plan["enabled"])
		self.assertEqual(plan["total_paid"], 200)
		self.assertEqual(plan["current_installment"]["sequence"], 2)
		self.assertEqual(plan["current_installment"]["shortfall"], 400)
		self.assertEqual(plan["installments"][1]["shortfall"], 400)

	def test_standard_overdue_reminder_skips_active_payment_plan(self):
		eligibility = overdue_reminder_eligibility(invoice(), today=date(2026, 7, 12))

		self.assertFalse(eligibility["eligible"])
		self.assertEqual(eligibility["reason_code"], "payment_plan")

	@patch("qas_custom.modules.notifications.invoice_payment_plan_reminders.frappe.get_all")
	@patch("qas_custom.modules.notifications.invoice_payment_plan_reminders.frappe.get_meta")
	def test_installment_reminders_respect_three_day_interval_and_five_attempt_limit(self, mock_meta, mock_get_all):
		mock_meta.return_value.has_field.return_value = True
		mock_get_all.return_value = [{"creation": datetime(2026, 7, 10, 9, 0)}]
		self.assertTrue(_recent_or_max_attempt("invoice_payment_plan_reminder:ACC-SINV-1:1:2026-07-12", date(2026, 7, 12)))

		mock_get_all.return_value = [{"creation": datetime(2026, 7, 1, 9, 0)} for _ in range(5)]
		self.assertTrue(_recent_or_max_attempt("invoice_payment_plan_reminder:ACC-SINV-1:1:2026-07-20", date(2026, 7, 20)))
