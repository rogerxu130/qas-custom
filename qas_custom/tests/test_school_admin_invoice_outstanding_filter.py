from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin import _apply_invoice_outstanding_amount_filter


class TestSchoolAdminInvoiceOutstandingFilter(TestCase):
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_applies_inclusive_range_for_exact_or_bounded_amounts(self, _has_field, _translate):
		filters = {}
		_apply_invoice_outstanding_amount_filter(filters, outstanding_min="68", outstanding_max="68.00")
		self.assertEqual(filters["outstanding_amount"], ["between", [68.0, 68.0]])

	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_applies_one_sided_minimum(self, _has_field, _translate):
		filters = {}
		_apply_invoice_outstanding_amount_filter(filters, outstanding_min="500")
		self.assertEqual(filters["outstanding_amount"], [">=", 500.0])

	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=ValueError("invalid range"))
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_rejects_reversed_range(self, _translate, _throw):
		with self.assertRaisesRegex(ValueError, "invalid range"):
			_apply_invoice_outstanding_amount_filter({}, outstanding_min="100", outstanding_max="68")
