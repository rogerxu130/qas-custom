from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.makeup.commands import (
	_get_makeup_booking_attendance,
	cancel_makeup_booking_core,
)
from qas_custom.services.school_admin import cancel_school_admin_makeup_booking_data


class FakeDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.flags = SimpleNamespace()
		self.saved = False

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, ignore_permissions=False):
		self.saved = ignore_permissions


class TestSchoolAdminMakeupCancellation(TestCase):
	@patch("qas_custom.modules.makeup.commands._build_makeup_voucher_payload")
	@patch("qas_custom.modules.makeup.commands._build_redeem_session_payload")
	@patch("qas_custom.modules.makeup.commands._get_makeup_booking_attendance")
	def test_cancel_restores_voucher_and_cancels_attendance(self, mock_attendance, mock_session, mock_voucher_payload):
		voucher = FakeDoc(
			name="MV-001",
			status="Used",
			student="STU-001",
			used_by_student="STU-002",
			used_on_session="CS-001",
			used_date="2026-07-19",
		)
		attendance = FakeDoc(name="ATT-001", status="To be started")
		mock_attendance.return_value = attendance
		mock_session.return_value = {"session_id": "CS-001"}
		mock_voucher_payload.side_effect = lambda doc: {"voucher_id": doc.name, "status": doc.status}

		fake_db = SimpleNamespace(has_column=Mock(return_value=True))
		with patch("qas_custom.modules.makeup.commands.frappe.db", fake_db):
			result = cancel_makeup_booking_core(voucher, confirm_cancel=1)

		self.assertEqual(attendance.status, "Cancelled")
		self.assertTrue(attendance.saved)
		self.assertEqual(voucher.status, "Valid")
		self.assertIsNone(voucher.used_on_session)
		self.assertIsNone(voucher.used_date)
		self.assertIsNone(voucher.used_by_student)
		self.assertTrue(voucher.saved)
		self.assertEqual(result["attendance"]["status_before"], "To be started")
		self.assertFalse(result["attendance"]["requires_marked_attendance_override"])

	@patch("qas_custom.modules.makeup.commands._build_makeup_voucher_payload", return_value={"voucher_id": "MV-001", "status": "Valid"})
	@patch("qas_custom.modules.makeup.commands._build_redeem_session_payload", return_value={"session_id": "CS-001"})
	@patch("qas_custom.modules.makeup.commands._get_makeup_booking_attendance")
	def test_marked_attendance_can_be_cancelled_after_confirmation(self, mock_attendance, _mock_session, _mock_payload):
		voucher = FakeDoc(name="MV-001", status="Used", student="STU-001", used_on_session="CS-001", used_date="2026-07-19")
		attendance = FakeDoc(name="ATT-001", status="Present")
		mock_attendance.return_value = attendance

		fake_db = SimpleNamespace(has_column=Mock(return_value=False))
		with patch("qas_custom.modules.makeup.commands.frappe.db", fake_db):
			result = cancel_makeup_booking_core(voucher, confirm_cancel=1)

		self.assertEqual(attendance.status, "Cancelled")
		self.assertTrue(result["attendance"]["requires_marked_attendance_override"])

	def test_cancellation_requires_confirmation(self):
		voucher = FakeDoc(name="MV-001", status="Used", student="STU-001", used_on_session="CS-001")
		with patch("qas_custom.modules.makeup.commands.frappe.throw", side_effect=RuntimeError("confirmation required")):
			with self.assertRaisesRegex(RuntimeError, "confirmation required"):
				cancel_makeup_booking_core(voucher, confirm_cancel=0)

	def test_non_used_voucher_is_rejected(self):
		voucher = FakeDoc(name="MV-001", status="Valid", student="STU-001")
		with patch("qas_custom.modules.makeup.commands.frappe.throw", side_effect=RuntimeError("not used")):
			with self.assertRaisesRegex(RuntimeError, "not used"):
				cancel_makeup_booking_core(voucher, confirm_cancel=1)

	def test_ambiguous_attendance_is_rejected(self):
		voucher = FakeDoc(name="MV-001")
		rows = [
			{"name": "ATT-001", "course_session": "CS-001", "student": "STU-001", "status": "Cancelled"},
			{"name": "ATT-002", "course_session": "CS-001", "student": "STU-001", "status": "Present"},
		]
		fake_frappe = SimpleNamespace(
			get_all=Mock(side_effect=[rows, []]),
			get_doc=Mock(),
			throw=Mock(side_effect=RuntimeError("ambiguous")),
			db=SimpleNamespace(has_column=Mock(return_value=True)),
		)
		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "ambiguous"):
				_get_makeup_booking_attendance(voucher, "CS-001", "STU-001")

		fake_frappe.get_doc.assert_not_called()

	@patch("qas_custom.services.school_admin._require_school_admin")
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin.cancel_makeup_booking_core", side_effect=RuntimeError("save failed"))
	def test_service_rolls_back_when_core_fails(self, _mock_core, mock_context, _mock_require):
		mock_context.return_value = (FakeDoc(name="PAR-001"), [], FakeDoc(name="MV-001"))
		fake_db = SimpleNamespace(commit=Mock(), rollback=Mock())
		with patch("qas_custom.services.school_admin.frappe.db", fake_db):
			with self.assertRaisesRegex(RuntimeError, "save failed"):
				cancel_school_admin_makeup_booking_data(parent="PAR-001", voucher_id="MV-001", confirm_cancel=1)

		fake_db.rollback.assert_called_once_with()
		fake_db.commit.assert_not_called()
