from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin import _get_invoice_outstanding_amount_filters


class TestSchoolAdminInvoiceOutstandingFilter(TestCase):
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_builds_two_numeric_conditions_for_a_bounded_amount(self, _has_field, _translate):
		filters = _get_invoice_outstanding_amount_filters(outstanding_min="68", outstanding_max="68.00")
		self.assertEqual(filters, [["outstanding_amount", ">=", 68.0], ["outstanding_amount", "<=", 68.0]])

	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_applies_one_sided_minimum(self, _has_field, _translate):
		filters = _get_invoice_outstanding_amount_filters(outstanding_min="500")
		self.assertEqual(filters, [["outstanding_amount", ">=", 500.0]])

	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=ValueError("invalid range"))
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_rejects_reversed_range(self, _translate, _throw):
		with self.assertRaisesRegex(ValueError, "invalid range"):
			_get_invoice_outstanding_amount_filters(outstanding_min="100", outstanding_max="68")
