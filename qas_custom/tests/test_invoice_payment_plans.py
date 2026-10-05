from datetime import date, datetime
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from qas_custom.modules.billing.payment_plans import apply_payment_plan, payment_plan_payload, validate_payment_plan
from qas_custom.modules.notifications.invoice_overdue_reminders import overdue_reminder_eligibility
from qas_custom.modules.notifications.invoice_payment_plan_reminders import _message, _recent_or_max_attempt
from qas_custom.modules.notifications.commands import _invoice_payment_plan_html, _invoice_pdf_payment_plan_block


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
	def setUp(self):
		# These are unit tests; translation, date preferences and enrollment
		# lookups do not need a live Frappe site or database.
		for module in (
			"qas_custom.modules.billing.payment_plans",
			"qas_custom.modules.notifications.invoice_overdue_reminders",
		):
			self.enterContext(patch(module + "._", side_effect=lambda value: value))
		self.enterContext(patch("qas_custom.modules.billing.payment_plans._last_linked_enrollment_session_date", return_value=None))
		self.enterContext(patch("qas_custom.modules.notifications.commands.formatdate", side_effect=lambda value: str(value)))
		def throw(message):
			raise frappe.ValidationError(message)
		self.enterContext(patch("qas_custom.modules.billing.payment_plans.frappe.throw", side_effect=throw))
		self.enterContext(patch("qas_custom.modules.billing.payment_plans.now_datetime", return_value=datetime(2026, 7, 1)))

	def test_plan_validation_accepts_outstanding_after_credit_or_payment(self):
		for outstanding in (600, 500, 350):
			with self.subTest(outstanding=outstanding):
				doc = invoice(outstanding_amount=outstanding, qas_has_payment_plan=0, qas_payment_plan_installments=[])
				rows = validate_payment_plan(doc, [
					{"due_date": "2026-07-10", "cumulative_amount_due": 100},
					{"due_date": "2026-07-20", "cumulative_amount_due": outstanding},
				])
				self.assertEqual(rows[-1]["cumulative_amount_due"], outstanding)

	def test_plan_validation_rejects_original_total_and_stale_balance(self):
		for target in (600, 500):
			with self.subTest(target=target), self.assertRaisesRegex(frappe.ValidationError, "current outstanding balance"):
				validate_payment_plan(invoice(outstanding_amount=400, qas_has_payment_plan=0, qas_payment_plan_installments=[]), [
					{"due_date": "2026-07-10", "cumulative_amount_due": 100},
					{"due_date": "2026-07-20", "cumulative_amount_due": target},
				])

	def test_plan_validation_rejects_draft_paid_and_existing_plan(self):
		rows = [{"due_date": "2026-07-10", "cumulative_amount_due": 100}, {"due_date": "2026-07-20", "cumulative_amount_due": 400}]
		for overrides, message in (({"docstatus": 0}, "submitted"), ({"outstanding_amount": 0}, "paid invoice"), ({}, "already been set")):
			with self.subTest(overrides=overrides), self.assertRaisesRegex(frappe.ValidationError, message):
				validate_payment_plan(invoice(**overrides), rows)

	def test_plan_validation_keeps_schedule_constraints(self):
		doc = invoice(qas_has_payment_plan=0, qas_payment_plan_installments=[])
		for rows, message in (
			([{"due_date": "2026-07-10", "cumulative_amount_due": 400}], "2 or 3"),
			([{"due_date": "2026-07-20", "cumulative_amount_due": 100}, {"due_date": "2026-07-10", "cumulative_amount_due": 400}], "later"),
			([{"due_date": "2026-07-10", "cumulative_amount_due": 400}, {"due_date": "2026-07-20", "cumulative_amount_due": 400}], "greater"),
		):
			with self.subTest(message=message), self.assertRaisesRegex(frappe.ValidationError, message):
				validate_payment_plan(doc, rows)
		with patch("qas_custom.modules.billing.payment_plans._last_linked_enrollment_session_date", return_value=date(2026, 7, 19)):
			with self.assertRaisesRegex(frappe.ValidationError, "last scheduled class"):
				validate_payment_plan(doc, [{"due_date": "2026-07-10", "cumulative_amount_due": 100}, {"due_date": "2026-07-20", "cumulative_amount_due": 400}])

	def test_outstanding_plan_excludes_pre_plan_credit_and_tracks_later_reductions(self):
		rows = [frappe._dict({"due_date": "2026-07-10", "cumulative_amount_due": 200}), frappe._dict({"due_date": "2026-07-20", "cumulative_amount_due": 500})]
		for outstanding, paid, shortfall in ((500, 0, 200), (450, 50, 150), (300, 200, 0), (0, 500, 0)):
			with self.subTest(outstanding=outstanding):
				plan = payment_plan_payload(invoice(outstanding_amount=outstanding, qas_payment_plan_installments=rows), today=date(2026, 7, 12))
				self.assertEqual(plan["total"], 500)
				self.assertEqual(plan["total_paid"], paid)
				self.assertEqual(plan["installments"][0]["shortfall"], shortfall)
				self.assertEqual(plan["installments"][0]["is_due"], shortfall > 0)
				if outstanding == 0:
					self.assertFalse(plan["enabled"])
					self.assertEqual(plan["status"], "Completed")

	@patch("qas_custom.modules.billing.payment_plans.frappe.get_doc")
	def test_save_validates_fresh_locked_balance(self, mock_get_doc):
		mock_get_doc.return_value = invoice(outstanding_amount=400, qas_has_payment_plan=0, qas_payment_plan_installments=[])
		with self.assertRaisesRegex(frappe.ValidationError, "current outstanding balance"):
			apply_payment_plan(invoice(outstanding_amount=500), [{"due_date": "2026-07-10", "cumulative_amount_due": 100}, {"due_date": "2026-07-20", "cumulative_amount_due": 500}])
		mock_get_doc.assert_called_once_with("Sales Invoice", "ACC-SINV-2026-00001", for_update=True)

	@patch("qas_custom.modules.billing.payment_plans.frappe.get_doc")
	def test_save_stores_outstanding_targets(self, mock_get_doc):
		doc = MagicMock()
		values = invoice(qas_has_payment_plan=0, qas_payment_plan_installments=[])
		doc.docstatus = 1
		doc.get.side_effect = values.get
		mock_get_doc.return_value = doc
		apply_payment_plan(values, [{"due_date": "2026-07-10", "cumulative_amount_due": 150}, {"due_date": "2026-07-20", "cumulative_amount_due": 400}], actor="admin")
		self.assertEqual(doc.append.call_args_list[-1].args[1]["cumulative_amount_due"], 400)
		doc.save.assert_called_once_with(ignore_permissions=True)

	@patch("qas_custom.modules.notifications.invoice_payment_plan_reminders.parent_portal_invoice_link", return_value="https://example.test/invoice")
	def test_reminder_and_schedule_show_plan_progress_after_credit(self, _link):
		doc = invoice(outstanding_amount=500, qas_payment_plan_installments=[frappe._dict({"due_date": "2026-07-10", "cumulative_amount_due": 200}), frappe._dict({"due_date": "2026-07-20", "cumulative_amount_due": 500})])
		plan = payment_plan_payload(doc, today=date(2026, 7, 12))
		message = _message(doc, plan, plan["current_installment"])
		self.assertIn("Payment plan total: $500.00", message)
		self.assertIn("Paid towards plan: $0.00", message)
		self.assertIn("Current installment shortfall: $200.00", message)
		for html in (_invoice_pdf_payment_plan_block({"payment_plan": plan}), _invoice_payment_plan_html(plan)):
			self.assertIn("Paid towards plan: AUD $0.00", html)
			self.assertIn("AUD $500.00", html)

	def test_invoice_pdf_includes_active_payment_plan_schedule(self):
		plan = payment_plan_payload(invoice(), today=date(2026, 7, 12))
		with patch("qas_custom.modules.notifications.commands.formatdate", side_effect=str):
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
