from unittest import TestCase

from qas_custom.modules.notifications.commands import (
	_session_staff_notification_email_message,
	_session_staff_notification_event_key,
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
