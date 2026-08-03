from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.modules.billing.presentation import build_parent_invoice_context
from qas_custom.modules.notifications.commands import _invoice_pdf_html
from qas_custom.services.school_admin import (
	_apply_invoice_adjustments,
	_apply_invoice_items,
	_invoice_edit_totals,
)


class _Meta:
	def has_field(self, _fieldname):
		return True


class _Child(frappe._dict):
	meta = _Meta()

	def set(self, fieldname, value):
		self[fieldname] = value


class _Invoice(frappe._dict):
	meta = _Meta()

	def set(self, fieldname, value):
		self[fieldname] = value

	def append(self, fieldname, values):
		child = _Child(values)
		self.setdefault(fieldname, []).append(child)
		return child

	def remove(self, row):
		self["taxes"].remove(row)


class TestSchoolAdminDraftInvoiceAdjustments(TestCase):
	def test_zero_unit_price_remains_zero(self):
		invoice = _Invoice(items=[])

		_apply_invoice_items(
			invoice,
			[
				{
					"item_code": "Tuition Fee",
					"description": "Complimentary class",
					"qty": 2,
					"rate": 0,
				}
			],
		)

		self.assertEqual(invoice.get("items")[0].qty, 2)
		self.assertEqual(invoice.get("items")[0].rate, 0)

	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_adjustments_replace_only_qas_adjustment_rows(self, _has_field):
		ordinary_tax = _Child(
			description="GST",
			tax_amount=10,
			account_head="GST Payable",
			qas_is_invoice_adjustment=0,
		)
		old_adjustment = _Child(
			description="Old discount",
			tax_amount=-20,
			account_head="Tuition Income",
			cost_center="Main - QAS",
			qas_is_invoice_adjustment=1,
		)
		invoice = _Invoice(
			company="Queensland Art School",
			items=[_Child(income_account="Tuition Income", cost_center="Main - QAS")],
			taxes=[ordinary_tax, old_adjustment],
		)

		_apply_invoice_adjustments(
			invoice,
			[
				{"description": "Sibling discount", "amount": -30},
				{"description": "Materials", "amount": 15},
			],
		)

		self.assertIn(ordinary_tax, invoice.get("taxes"))
		self.assertNotIn(old_adjustment, invoice.get("taxes"))
		self.assertEqual(len(invoice.get("taxes")), 3)
		self.assertEqual(
			[(row.description, row.tax_amount) for row in invoice.get("taxes")[1:]],
			[("Sibling discount", -30), ("Materials", 15)],
		)
		self.assertTrue(all(row.qas_is_invoice_adjustment for row in invoice.get("taxes")[1:]))

	def test_edit_totals_separate_items_adjustments_and_other_charges(self):
		invoice = _Invoice(
			items=[_Child(amount=400), _Child(amount=80)],
			taxes=[
				_Child(tax_amount=-30, qas_is_invoice_adjustment=1),
				_Child(tax_amount=10, qas_is_invoice_adjustment=0),
			],
			total_taxes_and_charges=-20,
		)

		self.assertEqual(
			_invoice_edit_totals(invoice),
			{
				"item_subtotal": 480,
				"adjustment_total": -30,
				"other_charge_total": 10,
			},
		)

	@patch("qas_custom.modules.billing.presentation.get_invoice_settings", return_value={})
	@patch("qas_custom.modules.billing.presentation.get_invoice_payment_context", return_value={})
	@patch("qas_custom.modules.billing.presentation.get_invoice_total_amount", return_value=450)
	@patch(
		"qas_custom.modules.billing.presentation.resolve_invoice_print_amounts",
		return_value={"store_credit_applied": 0, "payable_amount": 450},
	)
	@patch("qas_custom.modules.billing.presentation._invoice_recipient_name", return_value="Taylor")
	@patch("qas_custom.modules.billing.payment_plans.payment_plan_payload", return_value={})
	def test_parent_context_exposes_adjustment_as_independent_line(
		self,
		_payment_plan,
		_recipient_name,
		_amounts,
		_total,
		_payment_context,
		_settings,
	):
		invoice = _Invoice(
			name="SINV-0001",
			customer_name="Taylor Family",
			qas_additional_description="Capacity-building support delivered through a small-group visual arts session.",
			items=[
				_Child(
					item_code="Tuition Fee",
					description="Term 3 course",
					student_display_name="Alex",
					qty=8,
					rate=60,
					amount=480,
				)
			],
			taxes=[
				_Child(
					description="Sibling discount",
					tax_amount=-30,
					qas_is_invoice_adjustment=1,
				)
			],
		)

		context = build_parent_invoice_context(invoice, include_portal_link=False)

		self.assertEqual(len(context["lines"]), 2)
		self.assertEqual(context["lines"][1]["description"], "Sibling discount")
		self.assertEqual(context["lines"][1]["amount"], -30)
		self.assertEqual(
			context["additional_description"],
			"Capacity-building support delivered through a small-group visual arts session.",
		)

	@patch("qas_custom.modules.notifications.commands._school_identity_pdf_html", return_value="")
	def test_parent_pdf_hides_unit_and_unit_price_and_shows_adjustment(self, _identity):
		html = _invoice_pdf_html(
			{
				"invoice": "SINV-0001",
				"school_name": "Queensland Art School",
				"due_date": "31 July 2026",
				"posting_date": "24 July 2026",
				"total": 450,
				"store_credit_applied": 0,
				"payable_amount": 450,
				"invoice_message": "",
				"additional_description": "Capacity-building support\nwith peers.",
				"accepted_payment_methods": "",
				"lines": [
					{"student": "Alex", "description": "Term 3 course", "amount": 480},
					{"student": "", "description": "Sibling discount", "amount": -30},
				],
			}
		)

		self.assertIn("Sibling discount", html)
		self.assertIn("Additional description", html)
		self.assertIn("Capacity-building support<br>with peers.", html)
		self.assertNotIn("Unit price", html)
		self.assertNotIn(">Qty<", html)
		self.assertNotIn(">Rate<", html)
