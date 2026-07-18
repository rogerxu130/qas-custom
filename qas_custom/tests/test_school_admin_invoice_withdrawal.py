from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qas_custom.services.school_admin_import import (
	ATTENDANCE_DOCTYPE,
	HISTORICAL_ATTENDANCE_BLOCKING_STATUSES,
	INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW,
	_build_invoice_enrollment_reset_operation,
	_apply_enrollment_change_invoice_action,
	_count_historical_enrollment_attendance,
	_invoice_enrollment_reset_preview_snapshot,
	_invoice_enrollment_reset_requires_historical_attendance_confirmation,
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
		self.assertEqual(operation["row"]["send_notifications"], 1)
		self.assertEqual(operation["row"]["confirm_historical_attendance"], 0)

	def test_reset_can_skip_parent_notification(self):
		fake_frappe = SimpleNamespace(db=SimpleNamespace(exists=Mock(return_value=True)))
		with patch("qas_custom.services.school_admin_import.frappe", fake_frappe):
			operation = _build_invoice_enrollment_reset_operation({
				"invoice": "SINV-0001",
				"mode": INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW,
				"send_notifications": 0,
				"effective_date": "2026-07-16",
			})

		self.assertEqual(operation["row"]["send_notifications"], 0)

	@patch("qas_custom.services.school_admin_import.cancel_school_admin_invoice_data")
	@patch("qas_custom.services.school_admin_import._", side_effect=lambda value: value)
	def test_submitted_invoice_action_passes_notification_choice(self, _mock_translate, mock_cancel):
		_apply_enrollment_change_invoice_action(
			{"action": "cancel_submitted", "invoice": "SINV-0001"},
			"Family withdrew",
			allow_empty_reason=True,
			send_notifications=0,
		)

		mock_cancel.assert_called_once_with(
			invoice="SINV-0001",
			reason="Family withdrew",
			allow_empty_reason=True,
			send_notifications=0,
		)

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

	def test_historical_attendance_requires_confirmation(self):
		row = {"confirm_historical_attendance": 0}
		preview = {"counts": {"historical_attendance_found": 1}}

		self.assertTrue(_invoice_enrollment_reset_requires_historical_attendance_confirmation(row, preview))

	def test_historical_attendance_with_confirmation_is_allowed(self):
		row = {"confirm_historical_attendance": 1}
		preview = {"counts": {"historical_attendance_found": 1}}

		self.assertFalse(_invoice_enrollment_reset_requires_historical_attendance_confirmation(row, preview))

	def test_no_historical_attendance_needs_no_confirmation(self):
		row = {"confirm_historical_attendance": 0}
		preview = {"counts": {"historical_attendance_found": 0}}

		self.assertFalse(_invoice_enrollment_reset_requires_historical_attendance_confirmation(row, preview))

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

	def test_preview_snapshot_changes_when_notification_choice_changes(self):
		preview = {"input": {"student_count": 0}, "parents": []}

		self.assertNotEqual(
			_invoice_enrollment_reset_preview_snapshot({"send_notifications": 1}, preview),
			_invoice_enrollment_reset_preview_snapshot({"send_notifications": 0}, preview),
		)

	def test_preview_snapshot_changes_when_historical_attendance_changes(self):
		row = {"invoice": "SINV-0001", "effective_date": "2026-07-16"}
		preview = {
			"input": {"student_count": 1},
			"counts": {"historical_attendance_found": 1},
			"parents": [{"enrollment": "ENR-0001", "counts": {"historical_attendance_found": 1}}],
		}
		changed_preview = {
			**preview,
			"counts": {"historical_attendance_found": 2},
			"parents": [{"enrollment": "ENR-0001", "counts": {"historical_attendance_found": 2}}],
		}

		self.assertNotEqual(
			_invoice_enrollment_reset_preview_snapshot(row, preview),
			_invoice_enrollment_reset_preview_snapshot(row, changed_preview),
		)
