from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.qas_custom.doctype.invoice_discount_template.invoice_discount_template import InvoiceDiscountTemplate
from qas_custom.services.invoice_discount_templates import (
	_require_school_admin,
	get_school_admin_invoice_discount_templates_data,
	save_school_admin_invoice_discount_template_data,
)


class TestInvoiceDiscountTemplateValidation(TestCase):
	def template(self, **overrides):
		values = {
			"template_name": "Sibling discount",
			"description": "Sibling discount",
			"discount_type": "Fixed Amount",
			"discount_value": 30,
			"status": "Active",
		}
		values.update(overrides)
		return SimpleNamespace(**values)

	@patch("qas_custom.qas_custom.doctype.invoice_discount_template.invoice_discount_template._", side_effect=lambda value: value)
	@patch("qas_custom.qas_custom.doctype.invoice_discount_template.invoice_discount_template.frappe.throw", side_effect=frappe.ValidationError)
	def test_percentage_must_not_exceed_one_hundred(self, _throw, _translate):
		with self.assertRaises(frappe.ValidationError):
			InvoiceDiscountTemplate.validate(self.template(discount_type="Percentage", discount_value=101))

	def test_valid_template_is_trimmed(self):
		doc = self.template(template_name="  Holiday offer  ", description="  Holiday discount  ")
		InvoiceDiscountTemplate.validate(doc)
		self.assertEqual(doc.template_name, "Holiday offer")
		self.assertEqual(doc.description, "Holiday discount")


class TestInvoiceDiscountTemplateService(TestCase):
	@patch("qas_custom.services.invoice_discount_templates._", side_effect=lambda value: value)
	@patch("qas_custom.services.invoice_discount_templates.frappe")
	def test_guest_cannot_manage_templates(self, discount_frappe, _translate):
		discount_frappe.session.user = "Guest"
		discount_frappe.throw.side_effect = frappe.PermissionError

		with self.assertRaises(frappe.PermissionError):
			_require_school_admin()

	@patch("qas_custom.services.invoice_discount_templates._require_school_admin")
	@patch("qas_custom.services.invoice_discount_templates.frappe")
	def test_active_selector_excludes_inactive_templates(self, discount_frappe, _require):
		discount_frappe.get_all.return_value = [
			frappe._dict(name="IDT-1", template_name="Sibling", description="Sibling discount", discount_type="Fixed Amount", discount_value=30, status="Active")
		]

		result = get_school_admin_invoice_discount_templates_data()

		self.assertEqual(result["items"][0]["discount_value"], 30)
		self.assertEqual(discount_frappe.get_all.call_args.kwargs["filters"], {"status": "Active"})

	@patch("qas_custom.services.invoice_discount_templates._require_school_admin")
	@patch("qas_custom.services.invoice_discount_templates.frappe")
	def test_manager_can_include_inactive_templates(self, discount_frappe, _require):
		discount_frappe.get_all.return_value = []

		get_school_admin_invoice_discount_templates_data(include_inactive=1)

		self.assertEqual(discount_frappe.get_all.call_args.kwargs["filters"], {})

	@patch("qas_custom.services.invoice_discount_templates._require_school_admin")
	@patch("qas_custom.services.invoice_discount_templates.frappe")
	def test_save_creates_active_template(self, discount_frappe, _require):
		doc = frappe._dict(name="IDT-1", status="")
		doc.set = lambda field, value: doc.update({field: value})
		doc.is_new = Mock(return_value=True)
		doc.save = Mock()
		discount_frappe.new_doc.return_value = doc

		result = save_school_admin_invoice_discount_template_data(
			payload={
				"template_name": "Holiday",
				"description": "Holiday promotion",
				"discount_type": "Percentage",
				"discount_value": 20,
			},
		)

		self.assertEqual(doc.status, "Active")
		self.assertEqual(result["template"]["discount_value"], 20)
		doc.save.assert_called_once_with(ignore_permissions=True)
		discount_frappe.db.commit.assert_called_once()
