from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.commands import (
	TRIAL_ADDED_NOTIFICATION_DISABLED_REASON,
	_session_staff_notification_email_message,
	_session_staff_notification_event_key,
	enqueue_session_staff_notification,
	send_session_staff_notification_job,
	session_staff_notification_enabled,
)


class TestSessionStaffNotifications(TestCase):
	@patch("qas_custom.modules.notifications.commands.frappe.conf", {})
	def test_trial_added_notification_is_enabled_by_default(self):
		self.assertTrue(session_staff_notification_enabled("trial_added"))
		self.assertTrue(session_staff_notification_enabled("leave_requested"))

	@patch("qas_custom.modules.notifications.commands.frappe.conf", {"qas_trial_added_notification_enabled": 0})
	def test_trial_added_notification_can_be_disabled_without_affecting_other_events(self):
		self.assertFalse(session_staff_notification_enabled("trial_added"))
		self.assertTrue(session_staff_notification_enabled("leave_requested"))
		self.assertTrue(session_staff_notification_enabled("makeup_booked"))

	@patch("qas_custom.modules.notifications.commands._session_staff_notification_context")
	@patch("qas_custom.modules.notifications.commands.frappe.conf", {"qas_trial_added_notification_enabled": 0})
	def test_disabled_trial_added_notification_is_not_queued(self, mock_context):
		result = enqueue_session_staff_notification(
			"trial_added",
			course_session="CS-001",
			student="STU-001",
			source_doctype="Inquiry",
			source_document="INQ-001",
		)

		self.assertFalse(result["queued"])
		self.assertTrue(result["skipped"])
		self.assertEqual(result["reason"], TRIAL_ADDED_NOTIFICATION_DISABLED_REASON)
		mock_context.assert_not_called()

	@patch("qas_custom.modules.notifications.commands.send_session_staff_notification")
	@patch("qas_custom.modules.notifications.commands._mark_notification_failed")
	@patch("qas_custom.modules.notifications.commands.frappe.conf", {"qas_trial_added_notification_enabled": 0})
	def test_already_queued_trial_added_job_rechecks_the_switch(self, mock_mark_failed, mock_send):
		result = send_session_staff_notification_job(
			"trial_added",
			course_session="CS-001",
			student="STU-001",
			source_doctype="Inquiry",
			source_document="INQ-001",
			notification_log="NOTIF-001",
		)

		self.assertFalse(result["sent"])
		self.assertTrue(result["skipped"])
		mock_mark_failed.assert_called_once_with("NOTIF-001", TRIAL_ADDED_NOTIFICATION_DISABLED_REASON)
		mock_send.assert_not_called()

	def test_event_keys_are_stable_short_and_specific_to_the_source_event(self):
		leave_key = _session_staff_notification_event_key("leave_requested", "CS-001", "STU-001", "LR-001")
		makeup_key = _session_staff_notification_event_key("makeup_booked", "CS-001", "STU-001", "MV-001")
		trial_key = _session_staff_notification_event_key("trial_added", "CS-001", "STU-001", "INQ-001")

		self.assertEqual(
			makeup_key,
			_session_staff_notification_event_key("makeup_booked", "CS-001", "STU-001", "MV-001"),
		)
		self.assertTrue(leave_key.startswith("session_staff:leave:"))
		self.assertTrue(makeup_key.startswith("session_staff:makeup:"))
		self.assertTrue(trial_key.startswith("session_staff:trial:"))
		self.assertEqual(len({leave_key, makeup_key, trial_key}), 3)
		self.assertLessEqual(max(len(leave_key), len(makeup_key), len(trial_key)), 140)

		long_makeup_key = _session_staff_notification_event_key(
			"makeup_booked",
			"New Test Term-Anime Art - Intermediate-Indooroopilly-Thursday-13:00:00-Roger Xu-2026-08-06",
			"Isabella 1-2018-05-31",
			"MV-Anime Art - Intermediate-Isabella 1-2018-05-31-2026-07-14-0074",
		)
		self.assertLessEqual(len(long_makeup_key), 140)

	def test_trial_email_includes_session_details_and_escapes_student_name(self):
		message = _session_staff_notification_email_message(
			{
				"event": "trial_added",
				"school_name": "Queensland Art School",
				"student_name": "Ava & Ben",
				"course": "Anime Art",
				"campus": "Indooroopilly",
				"classroom": "Room 1",
				"day_of_week": "Tuesday",
				"date_display": "14 July 2026",
				"start_time": "16:00",
				"end_time": "17:30",
			}
		)

		self.assertIn("Ava &amp; Ben", message)
		self.assertIn("This session now has a trial student.", message)
		self.assertIn("Indooroopilly", message)
		self.assertIn("Room 1", message)
		self.assertIn("Tuesday 14 July 2026", message)
		self.assertIn("16:00 - 17:30", message)

	@patch("qas_custom.modules.notifications.commands.frappe.enqueue")
	@patch("qas_custom.modules.notifications.commands.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.commands._mark_notification_queued")
	@patch("qas_custom.modules.notifications.commands._create_notification_log", return_value="NOTIF-001")
	@patch("qas_custom.modules.notifications.commands._session_staff_notification_already_logged", return_value=False)
	@patch("qas_custom.modules.notifications.commands._session_staff_notification_email_message", return_value="message")
	@patch("qas_custom.modules.notifications.commands._session_staff_notification_subject", return_value="subject")
	@patch("qas_custom.modules.notifications.commands._session_staff_notification_context")
	def test_queued_job_uses_a_non_reserved_event_keyword(
		self,
		mock_context,
		_mock_subject,
		_mock_message,
		_mock_already_logged,
		_mock_create_log,
		_mock_mark_queued,
		_mock_outbound_email_enabled,
		mock_enqueue,
	):
		mock_context.return_value = {
			"recipients": ["teacher@example.com"],
			"missing_recipients": [],
			"event": "leave_requested",
		}

		enqueue_session_staff_notification(
			"leave_requested",
			course_session="CS-001",
			student="STU-001",
			source_doctype="Leave Request",
			source_document="LR-001",
		)

		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["notification_event"], "leave_requested")
		self.assertNotIn("event", kwargs)
