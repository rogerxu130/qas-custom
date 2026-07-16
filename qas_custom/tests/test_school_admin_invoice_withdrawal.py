from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qas_custom.services.school_admin_import import (
	ATTENDANCE_DOCTYPE,
	HISTORICAL_ATTENDANCE_BLOCKING_STATUSES,
	INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW,
	_build_invoice_enrollment_reset_operation,
	_count_historical_enrollment_attendance,
	_invoice_enrollment_reset_preview_snapshot,
	_invoice_enrollment_reset_requires_multiple_withdrawal_confirmation,
)


class TestSchoolAdminInvoiceWithdrawal(TestCase):
	@patch("qas_custom.services.school_admin_import._doctype_available", return_value=True)
	def test_only_present_and_late_attendance_block_reset(self, _mock_doctype_available):
		fake_db = SimpleNamespace(count=Mock(return_value=2))
		fake_frappe = SimpleNamespace(db=fake_db)

		with patch("qas_custom.services.school_admin_import.frappe", fake_frappe):
			count = _count_historical_enrollment_attendance("ENR-0001")

		self.assertEqual(set(HISTORICAL_ATTENDANCE_BLOCKING_STATUSES), {"Present", "Late"})
		self.assertEqual(count, 2)
		fake_db.count.assert_called_once_with(ATTENDANCE_DOCTYPE, {
			"source_doctype": "Enrollment",
			"source_document": "ENR-0001",
			"status": ["in", ["Present", "Late"]],
		})

	def test_reset_reason_is_optional(self):
		fake_frappe = SimpleNamespace(db=SimpleNamespace(exists=Mock(return_value=True)))
		with patch("qas_custom.services.school_admin_import.frappe", fake_frappe):
			operation = _build_invoice_enrollment_reset_operation({
				"invoice": "SINV-0001",
				"mode": INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW,
				"reason": "",
				"effective_date": "2026-07-16",
			})

		self.assertEqual(operation["row"]["reason"], "")
		self.assertEqual(operation["row"]["errors"], [])

	def test_one_student_with_multiple_enrollments_does_not_require_confirmation(self):
		row = {"mode": INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW, "confirm_multiple_withdrawal": 0}
		preview = {"input": {"student_count": 1, "enrollment_count": 3}}

		self.assertFalse(_invoice_enrollment_reset_requires_multiple_withdrawal_confirmation(row, preview))

	def test_two_students_require_confirmation(self):
		row = {"mode": INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW, "confirm_multiple_withdrawal": 0}
		preview = {"input": {"student_count": 2, "enrollment_count": 2}}

		self.assertTrue(_invoice_enrollment_reset_requires_multiple_withdrawal_confirmation(row, preview))

	def test_two_students_with_confirmation_are_allowed(self):
		row = {"mode": INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW, "confirm_multiple_withdrawal": 1}
		preview = {"input": {"student_count": 2, "enrollment_count": 2}}

		self.assertFalse(_invoice_enrollment_reset_requires_multiple_withdrawal_confirmation(row, preview))

	def test_preview_snapshot_changes_when_linked_student_changes(self):
		row = {"invoice": "SINV-0001", "reason": "", "effective_date": "2026-07-16"}
		preview = {
			"input": {"student_count": 1},
			"invoice_status": "Submitted",
			"invoice_action": "Cancel",
			"parents": [{"enrollment": "ENR-0001", "student": "STU-0001", "enrollment_status": "Active", "counts": {}}],
		}
		changed_preview = {
			**preview,
			"parents": [{"enrollment": "ENR-0001", "student": "STU-0002", "enrollment_status": "Active", "counts": {}}],
		}

		self.assertNotEqual(
			_invoice_enrollment_reset_preview_snapshot(row, preview),
			_invoice_enrollment_reset_preview_snapshot(row, changed_preview),
		)
