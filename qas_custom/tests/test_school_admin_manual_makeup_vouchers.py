from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.api.school_admin import (
	school_admin_cancel_unused_makeup_voucher,
	school_admin_issue_manual_makeup_voucher,
)
from qas_custom.services.school_admin import (
	cancel_school_admin_unused_makeup_voucher_data,
	issue_school_admin_manual_makeup_voucher_data,
)


class FakeDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.flags = SimpleNamespace()
		self.inserted = False
		self.saved = False

	def get(self, key, default=None):
		return getattr(self, key, default)

	def insert(self, ignore_permissions=False):
		self.inserted = ignore_permissions

	def save(self, ignore_permissions=False):
		self.saved = ignore_permissions


class TestSchoolAdminManualMakeupVouchers(TestCase):
	@patch("qas_custom.services.school_admin._build_school_admin_makeup_voucher_payload", return_value={"voucher_id": "MV-001"})
	@patch("qas_custom.services.school_admin.queue_makeup_voucher_issued_email", return_value={"queued": True})
	@patch("qas_custom.services.school_admin._add_comment")
	@patch("qas_custom.services.school_admin.sync_makeup_voucher_label")
	@patch("qas_custom.services.school_admin._school_admin_manual_voucher_expiry_date", return_value="2026-10-25")
	@patch("qas_custom.services.school_admin._school_admin_required_reason", return_value="Goodwill")
	@patch("qas_custom.services.school_admin._school_admin_require_active_course", return_value="COURSE-ANIME")
	@patch("qas_custom.services.school_admin._assert_student_in_family", return_value="STU-001")
	@patch("qas_custom.services.school_admin._get_school_admin_family_context")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_issue_creates_standalone_voucher_and_queues_parent_email(
		self,
		_require_admin,
		family_context,
		_assert_student,
		_require_course,
		_reason,
		_expiry,
		_sync_label,
		_add_comment,
		_queue_email,
		_payload,
	):
		parent = FakeDoc(name="PAR-001")
		created = FakeDoc(name="MV-001")
		refreshed = FakeDoc(name="MV-001", student="STU-001", course="COURSE-ANIME", status="Valid")
		family_context.return_value = (parent, [{"name": "STU-001"}])
		fake_db = SimpleNamespace(commit=Mock())
		fake_frappe = SimpleNamespace(
			new_doc=Mock(return_value=created),
			get_doc=Mock(return_value=refreshed),
			db=fake_db,
			session=SimpleNamespace(user="admin@example.com"),
		)

		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin.today", return_value="2026-07-27"
		):
			result = issue_school_admin_manual_makeup_voucher_data(
				parent="PAR-001",
				student="STU-001",
				course="COURSE-ANIME",
				expiry_date="2026-10-25",
				reason="Goodwill",
				notify_parent=1,
			)

		self.assertTrue(created.inserted)
		self.assertEqual(created.student, "STU-001")
		self.assertEqual(created.course, "COURSE-ANIME")
		self.assertEqual(created.status, "Valid")
		self.assertEqual(created.issue_date, "2026-07-27")
		self.assertEqual(created.expiry_date, "2026-10-25")
		self.assertFalse(hasattr(created, "original_session"))
		self.assertFalse(hasattr(created, "leave_request"))
		_queue_email.assert_called_once_with("MV-001")
		fake_db.commit.assert_called_once_with()
		self.assertEqual(result["parent"], "PAR-001")
		self.assertTrue(result["parent_notification"]["queued"])

	@patch("qas_custom.services.school_admin._build_school_admin_makeup_voucher_payload", return_value={"voucher_id": "MV-001"})
	@patch("qas_custom.services.school_admin.queue_makeup_voucher_issued_email")
	@patch("qas_custom.services.school_admin._add_comment")
	@patch("qas_custom.services.school_admin.sync_makeup_voucher_label")
	@patch("qas_custom.services.school_admin._school_admin_manual_voucher_expiry_date", return_value="2026-10-25")
	@patch("qas_custom.services.school_admin._school_admin_required_reason", return_value="Goodwill")
	@patch("qas_custom.services.school_admin._school_admin_require_active_course", return_value="COURSE-ANIME")
	@patch("qas_custom.services.school_admin._assert_student_in_family", return_value="STU-001")
	@patch("qas_custom.services.school_admin._get_school_admin_family_context", return_value=(FakeDoc(name="PAR-001"), [{"name": "STU-001"}]))
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_issue_can_skip_parent_email(
		self,
		_require_admin,
		_family_context,
		_assert_student,
		_require_course,
		_reason,
		_expiry,
		_sync_label,
		_add_comment,
		_queue_email,
		_payload,
	):
		created = FakeDoc(name="MV-001")
		refreshed = FakeDoc(name="MV-001", student="STU-001", course="COURSE-ANIME", status="Valid")
		fake_frappe = SimpleNamespace(
			new_doc=Mock(return_value=created),
			get_doc=Mock(return_value=refreshed),
			db=SimpleNamespace(commit=Mock()),
			session=SimpleNamespace(user="admin@example.com"),
		)

		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin.today", return_value="2026-07-27"
		):
			result = issue_school_admin_manual_makeup_voucher_data(
				parent="PAR-001",
				student="STU-001",
				course="COURSE-ANIME",
				reason="Goodwill",
				notify_parent=0,
			)

		_queue_email.assert_not_called()
		self.assertTrue(result["parent_notification"]["skipped"])

	@patch("qas_custom.services.school_admin._build_school_admin_makeup_voucher_payload", return_value={"voucher_id": "MV-001", "status": "Cancelled"})
	@patch("qas_custom.services.school_admin._add_comment")
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin._school_admin_required_reason", return_value="Issued in error")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_direct_cancel_changes_only_unused_valid_voucher(
		self,
		_require_admin,
		_reason,
		family_context,
		_add_comment,
		_payload,
	):
		voucher = FakeDoc(name="MV-001", status="Valid", student="STU-001")
		family_context.return_value = (FakeDoc(name="PAR-001"), [{"name": "STU-001"}], voucher)
		fake_db = SimpleNamespace(commit=Mock())
		fake_frappe = SimpleNamespace(db=fake_db, session=SimpleNamespace(user="admin@example.com"))

		with patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = cancel_school_admin_unused_makeup_voucher_data(
				parent="PAR-001",
				voucher_id="MV-001",
				reason="Issued in error",
				confirm_cancel=1,
			)

		self.assertEqual(voucher.status, "Cancelled")
		self.assertTrue(voucher.saved)
		self.assertTrue(voucher.flags.skip_makeup_attendance_sync)
		self.assertTrue(result["cancelled"])
		fake_db.commit.assert_called_once_with()

	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=RuntimeError("used voucher"))
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin._school_admin_required_reason", return_value="Issued in error")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_direct_cancel_rejects_used_voucher(
		self,
		_require_admin,
		_reason,
		family_context,
		_throw,
	):
		voucher = FakeDoc(name="MV-001", status="Valid", student="STU-001", used_on_session="CS-001")
		family_context.return_value = (FakeDoc(name="PAR-001"), [{"name": "STU-001"}], voucher)

		with self.assertRaisesRegex(RuntimeError, "used voucher"):
			cancel_school_admin_unused_makeup_voucher_data(
				parent="PAR-001",
				voucher_id="MV-001",
				reason="Issued in error",
				confirm_cancel=1,
			)

	@patch("qas_custom.api.school_admin.issue_school_admin_manual_makeup_voucher_data")
	def test_issue_api_forwards_request(self, service):
		service.return_value = {"voucher": {"voucher_id": "MV-001"}}

		result = school_admin_issue_manual_makeup_voucher.__wrapped__(
			parent="PAR-001",
			student="STU-001",
			course="COURSE-ANIME",
			expiry_date="2026-10-25",
			reason="Goodwill",
			notify_parent=0,
		)

		self.assertEqual(result["voucher"]["voucher_id"], "MV-001")
		service.assert_called_once_with(
			parent="PAR-001",
			student="STU-001",
			course="COURSE-ANIME",
			expiry_date="2026-10-25",
			reason="Goodwill",
			notify_parent=0,
		)

	@patch("qas_custom.api.school_admin.cancel_school_admin_unused_makeup_voucher_data")
	def test_cancel_api_forwards_confirmation(self, service):
		service.return_value = {"cancelled": True}

		result = school_admin_cancel_unused_makeup_voucher.__wrapped__(
			parent="PAR-001",
			voucher_id="MV-001",
			reason="Issued in error",
			confirm_cancel=1,
		)

		self.assertTrue(result["cancelled"])
		service.assert_called_once_with(
			parent="PAR-001",
			voucher_id="MV-001",
			reason="Issued in error",
			confirm_cancel=1,
		)
