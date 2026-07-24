from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.makeup.commands import _ensure_leave_makeup_voucher, redeem_parent_voucher_core
from qas_custom.modules.notifications.makeup_parent_notifications import (
	_makeup_event_is_current,
	makeup_booking_email_message,
	makeup_parent_event_key,
	makeup_voucher_issued_email_message,
	queue_makeup_booking_confirmation,
	queue_makeup_voucher_issued_email,
	send_makeup_parent_email_job,
)


class FakeDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.flags = values.get("flags") or SimpleNamespace()
		self.meta = values.get("meta") or SimpleNamespace(has_field=lambda _fieldname: True)
		self.saved = False
		self.inserted = False

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def as_dict(self):
		return dict(self.__dict__)

	def save(self, ignore_permissions=False):
		self.saved = ignore_permissions

	def insert(self, ignore_permissions=False):
		self.inserted = ignore_permissions


def parent_context(**overrides):
	values = {
		"event_kind": "voucher_issued",
		"voucher": "MV-001",
		"voucher_label": "25 Jul 2026 4:00 PM · Ava01 · Anime",
		"recipient": {"email": "parent@example.com", "for_user": "parent@example.com", "parent": "PAR-001"},
		"parent_name": "Taylor",
		"student": "STU-001",
		"student_name": "Ava",
		"course": "Anime - Beginner",
		"campus": "Indooroopilly",
		"classroom": "Room 2",
		"teacher_name": "Teacher One",
		"date_display": "25 July 2026",
		"start_time": "4:00 PM",
		"end_time": "5:30 PM",
		"expiry_date_display": "23 October 2026",
		"portal_url": "https://portal.example.com/vouchers",
		"school_name": "Queensland Art School",
		"school_email": "school@example.com",
		"course_session": "CS-001",
	}
	values.update(overrides)
	return values


class TestMakeupParentEmailContent(TestCase):
	def test_event_keys_are_stable_short_and_distinct(self):
		issued = makeup_parent_event_key("voucher_issued", "MV-001")
		booked = makeup_parent_event_key("booking_confirmed", "MV-001", "CS-001", "STU-001")

		self.assertEqual(issued, makeup_parent_event_key("voucher_issued", "MV-001"))
		self.assertNotEqual(issued, booked)
		self.assertLessEqual(len(issued), 140)
		self.assertLessEqual(len(booked), 140)

	def test_voucher_email_contains_details_and_portal_button(self):
		html = makeup_voucher_issued_email_message(parent_context())

		self.assertIn("Ava", html)
		self.assertIn("Anime - Beginner", html)
		self.assertIn("25 July 2026", html)
		self.assertIn("23 October 2026", html)
		self.assertIn("View Makeup Voucher", html)
		self.assertIn("https://portal.example.com/vouchers", html)

	def test_booking_email_contains_class_details_and_schedule_button(self):
		html = makeup_booking_email_message(
			parent_context(
				event_kind="booking_confirmed",
				portal_url="https://portal.example.com/schedule/STU-001",
			)
		)

		for value in [
			"Ava",
			"Anime - Beginner",
			"Indooroopilly",
			"Room 2",
			"Teacher One",
			"25 July 2026",
			"4:00 PM",
			"5:30 PM",
			"View Class Schedule",
			"https://portal.example.com/schedule/STU-001",
		]:
			self.assertIn(value, html)


class TestMakeupParentEmailQueue(TestCase):
	@patch("qas_custom.modules.notifications.makeup_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._reserve_notification_event", return_value="LOG-001")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._build_makeup_parent_context")
	def test_voucher_queue_is_after_commit_and_deduplicated(
		self,
		mock_context,
		_mock_exists,
		_mock_reserve,
		_mock_mark_queued,
		_mock_enabled,
		mock_enqueue,
	):
		mock_context.return_value = parent_context()

		result = queue_makeup_voucher_issued_email("MV-001")

		self.assertTrue(result["queued"])
		kwargs = mock_enqueue.call_args.kwargs
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["voucher"], "MV-001")
		self.assertEqual(kwargs["event_kind"], "voucher_issued")

	@patch("qas_custom.modules.notifications.makeup_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._notification_event_exists", return_value=True)
	def test_duplicate_booking_queue_is_skipped(self, _mock_exists, mock_enqueue):
		result = queue_makeup_booking_confirmation("MV-001", "CS-001", "STU-001")

		self.assertTrue(result["skipped"])
		self.assertTrue(result["duplicate"])
		mock_enqueue.assert_not_called()

	@patch("qas_custom.modules.notifications.makeup_parent_notifications._mark_notification_failed")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications.sendmail_or_skip", return_value={})
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._mark_notification_sent")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._refresh_notification_log")
	@patch("qas_custom.modules.notifications.makeup_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._notification_log_sent", return_value=False)
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._makeup_event_is_current", return_value=True)
	@patch("qas_custom.modules.notifications.makeup_parent_notifications._build_makeup_parent_context")
	def test_worker_sends_to_family_and_marks_log_sent(
		self,
		mock_context,
		_mock_current,
		_mock_already_sent,
		_mock_enabled,
		_mock_refresh,
		mock_mark_sent,
		mock_sendmail,
		mock_mark_failed,
	):
		mock_context.return_value = parent_context(
			event_kind="booking_confirmed",
			portal_url="https://portal.example.com/schedule/STU-001",
		)

		result = send_makeup_parent_email_job(
			event_kind="booking_confirmed",
			voucher="MV-001",
			course_session="CS-001",
			student="STU-001",
			notification_log="LOG-001",
		)

		self.assertTrue(result["sent"])
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["parent@example.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["reference_doctype"], "Makeup Voucher")
		self.assertEqual(mock_sendmail.call_args.kwargs["reference_name"], "MV-001")
		mock_mark_sent.assert_called_once_with("LOG-001")
		mock_mark_failed.assert_not_called()


class TestMakeupParentEmailStateValidation(TestCase):
	def test_booking_validation_supports_legacy_redeemed_student_field(self):
		voucher = frappe._dict(
			{
				"status": "Used",
				"student": "STU-OWNER",
				"redeemed_student": "STU-001",
				"used_on_session": "CS-001",
			}
		)
		fake_db = SimpleNamespace(
			exists=Mock(side_effect=[True, True]),
			has_column=Mock(
				side_effect=lambda doctype, fieldname: (
					fieldname == "redeemed_student"
					if doctype == "Makeup Voucher"
					else fieldname in {"source_doctype", "source_document"}
				)
			),
			get_value=Mock(return_value=voucher),
		)

		with patch(
			"qas_custom.modules.notifications.makeup_parent_notifications.frappe.db",
			fake_db,
		):
			self.assertTrue(
				_makeup_event_is_current(
					"booking_confirmed",
					"MV-001",
					"CS-001",
					"STU-001",
				)
			)

		self.assertIn("redeemed_student", fake_db.get_value.call_args.args[2])
		self.assertNotIn("used_by_student", fake_db.get_value.call_args.args[2])


class TestMakeupParentNotificationTriggers(TestCase):
	@patch("qas_custom.modules.makeup.commands.queue_makeup_voucher_issued_email")
	@patch("qas_custom.modules.makeup.commands.sync_makeup_voucher_label")
	@patch("qas_custom.modules.makeup.commands.today", return_value="2026-07-25")
	def test_new_voucher_queues_parent_email_once(
		self,
		_mock_today,
		_mock_label,
		mock_queue,
	):
		created = FakeDoc(name="MV-001")
		refreshed = FakeDoc(name="MV-001", status="Valid")
		mock_queue.return_value = {"queued": True}
		leave = FakeDoc(name="LR-001", student="STU-001", course="Anime", course_session="CS-001")
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(exists=Mock(return_value=None)),
			new_doc=Mock(return_value=created),
			get_doc=Mock(return_value=refreshed),
		)

		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			result = _ensure_leave_makeup_voucher(leave)

		self.assertIs(result, refreshed)
		self.assertTrue(created.inserted)
		mock_queue.assert_called_once_with("MV-001")

	@patch("qas_custom.modules.makeup.commands.queue_makeup_booking_confirmation")
	@patch("qas_custom.modules.makeup.commands._queue_makeup_notification", return_value={"queued": True})
	@patch("qas_custom.modules.makeup.commands._build_redeem_session_payload", return_value={"session_id": "CS-001"})
	@patch("qas_custom.modules.makeup.commands.get_makeup_voucher_label", return_value="Voucher")
	@patch("qas_custom.modules.makeup.commands._set_voucher_used_by_student")
	@patch("qas_custom.modules.makeup.commands.redeem_voucher_attendance_entry", return_value="ATT-001")
	@patch("qas_custom.modules.makeup.commands._validate_session_can_redeem_voucher")
	@patch("qas_custom.modules.makeup.commands._validate_voucher_available_for_redeem")
	@patch("qas_custom.modules.makeup.commands._get_redeem_student", return_value="STU-001")
	@patch("qas_custom.modules.makeup.commands._get_parent_makeup_voucher")
	@patch("qas_custom.modules.makeup.commands.today", return_value="2026-07-25")
	def test_new_booking_queues_parent_confirmation(
		self,
		_mock_today,
		mock_voucher,
		_mock_student,
		_mock_validate_voucher,
		_mock_validate_session,
		_mock_attendance,
		_mock_set_student,
		_mock_label,
		_mock_session,
		_mock_staff,
		mock_parent_queue,
	):
		mock_voucher.return_value = FakeDoc(
			name="MV-001",
			status="Valid",
			used_on_session=None,
			student="STU-001",
		)
		mock_parent_queue.return_value = {"queued": True}
		fake_frappe = SimpleNamespace(db=SimpleNamespace(has_column=Mock(return_value=False)))

		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			result = redeem_parent_voucher_core(
				parent=FakeDoc(name="PAR-001"),
				students=[{"name": "STU-001"}],
				voucher_id="MV-001",
				session_id="CS-001",
				student="STU-001",
			)

		mock_parent_queue.assert_called_once_with("MV-001", "CS-001", "STU-001")
		self.assertTrue(result["parent_notification"]["queued"])

	@patch("qas_custom.modules.makeup.commands.queue_makeup_booking_confirmation")
	@patch("qas_custom.modules.makeup.commands._queue_makeup_notification", return_value={"queued": True})
	@patch("qas_custom.modules.makeup.commands._build_redeem_session_payload", return_value={"session_id": "CS-001"})
	@patch("qas_custom.modules.makeup.commands.get_makeup_voucher_label", return_value="Voucher")
	@patch("qas_custom.modules.makeup.commands._set_voucher_used_by_student")
	@patch("qas_custom.modules.makeup.commands.redeem_voucher_attendance_entry", return_value="ATT-001")
	@patch("qas_custom.modules.makeup.commands._validate_session_can_redeem_voucher")
	@patch("qas_custom.modules.makeup.commands._validate_voucher_available_for_redeem")
	@patch("qas_custom.modules.makeup.commands._get_redeem_student", return_value="STU-001")
	@patch("qas_custom.modules.makeup.commands._get_parent_makeup_voucher")
	@patch("qas_custom.modules.makeup.commands.today", return_value="2026-07-25")
	def test_booking_can_defer_parent_confirmation_until_outer_transaction_completes(
		self,
		_mock_today,
		mock_voucher,
		_mock_student,
		_mock_validate_voucher,
		_mock_validate_session,
		_mock_attendance,
		_mock_set_student,
		_mock_label,
		_mock_session,
		_mock_staff,
		mock_parent_queue,
	):
		mock_voucher.return_value = FakeDoc(
			name="MV-001",
			status="Valid",
			used_on_session=None,
			student="STU-001",
		)
		fake_frappe = SimpleNamespace(db=SimpleNamespace(has_column=Mock(return_value=False)))

		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			result = redeem_parent_voucher_core(
				parent=FakeDoc(name="PAR-001"),
				students=[{"name": "STU-001"}],
				voucher_id="MV-001",
				session_id="CS-001",
				student="STU-001",
				notify_parent=False,
			)

		mock_parent_queue.assert_not_called()
		self.assertEqual(
			result["parent_notification"]["reason"],
			"deferred_until_makeup_transaction_completes",
		)

	@patch("qas_custom.modules.makeup.commands.queue_makeup_booking_confirmation")
	@patch("qas_custom.modules.makeup.commands._queue_makeup_notification", return_value={"queued": True})
	@patch("qas_custom.modules.makeup.commands._build_redeem_session_payload", return_value={"session_id": "CS-001"})
	@patch("qas_custom.modules.makeup.commands.get_makeup_voucher_label", return_value="Voucher")
	@patch("qas_custom.modules.makeup.commands._get_attendance_entry_used_by_voucher", return_value={"name": "ATT-001"})
	@patch("qas_custom.modules.makeup.commands._get_voucher_used_by_student", return_value="STU-001")
	@patch("qas_custom.modules.makeup.commands._get_redeem_student", return_value="STU-001")
	@patch("qas_custom.modules.makeup.commands._get_parent_makeup_voucher")
	def test_idempotent_existing_booking_does_not_resend_parent_email(
		self,
		mock_voucher,
		_mock_student,
		_mock_used_student,
		_mock_attendance,
		_mock_label,
		_mock_session,
		_mock_staff,
		mock_parent_queue,
	):
		mock_voucher.return_value = FakeDoc(
			name="MV-001",
			status="Used",
			used_on_session="CS-001",
			student="STU-001",
		)

		result = redeem_parent_voucher_core(
			parent=FakeDoc(name="PAR-001"),
			students=[{"name": "STU-001"}],
			voucher_id="MV-001",
			session_id="CS-001",
			student="STU-001",
		)

		mock_parent_queue.assert_not_called()
		self.assertTrue(result["parent_notification"]["skipped"])
