from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.commands import (
	_session_staff_notification_email_message,
	_session_staff_notification_event_key,
	enqueue_session_staff_notification,
)


class TestSessionStaffNotifications(TestCase):
	def test_event_keys_are_stable_and_specific_to_the_source_event(self):
		self.assertEqual(
			_session_staff_notification_event_key("leave_requested", "CS-001", "STU-001", "LR-001"),
			"session_staff:leave:LR-001",
		)
		self.assertEqual(
			_session_staff_notification_event_key("makeup_booked", "CS-001", "STU-001", "MV-001"),
			"session_staff:makeup:MV-001:CS-001:STU-001",
		)
		self.assertEqual(
			_session_staff_notification_event_key("trial_added", "CS-001", "STU-001", "INQ-001"),
			"session_staff:trial:INQ-001:CS-001",
		)

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
