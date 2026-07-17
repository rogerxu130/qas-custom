from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.teacher_session_completion_reminders import (
	TEACHER_PORTAL_URL,
	_build_completion_states,
	_is_in_send_window,
	_queue_session_completion_reminder,
	_select_sessions_in_send_window,
	_teacher_session_completion_message,
	_teacher_session_completion_subject,
	run_teacher_session_completion_reminders,
	send_teacher_session_completion_reminder_job,
	teacher_session_completion_event_key,
	teacher_session_completion_reminder_enabled,
)


class TestTeacherSessionCompletionReminders(TestCase):
	def setUp(self):
		self.sessions = [
			{
				"name": "CS-001",
				"weekly_timeslot": "WT-001",
				"session_date": "2026-07-17",
				"status": "Scheduled",
				"teacher_override": "TEA-OVERRIDE",
			}
		]
		self.timeslots = {
			"WT-001": {
				"name": "WT-001",
				"course": "Anime Art",
				"campus": "Indooroopilly",
				"classroom": "Studio 1",
				"teacher": "TEA-WEEKLY",
				"start_time": "16:00:00",
				"end_time": "17:30:00",
			}
		}

	def test_send_window_uses_brisbane_wall_clock_and_is_end_exclusive(self):
		brisbane_now = datetime(2026, 7, 17, 17, 15, tzinfo=timezone(timedelta(hours=10)))
		self.assertTrue(_is_in_send_window(brisbane_now, "2026-07-17", "17:30:00"))
		self.assertFalse(_is_in_send_window(brisbane_now, "2026-07-17", "17:30:01"))
		self.assertFalse(_is_in_send_window(brisbane_now, "2026-07-17", "17:15:00"))

	def test_selects_only_sessions_ending_within_fifteen_minutes(self):
		now = datetime(2026, 7, 17, 17, 20)
		due = _select_sessions_in_send_window(self.sessions, self.timeslots, now)
		self.assertEqual([row["name"] for row in due], ["CS-001"])

	def test_override_teacher_wins_and_unmarked_attendance_requires_reminder(self):
		states = _build_completion_states(
			self.sessions,
			self.timeslots,
			[
				{"course_session": "CS-001", "status": "Present"},
				{"course_session": "CS-001", "status": "To be started"},
				{"course_session": "CS-001", "status": "Leave"},
				{"course_session": "CS-001", "status": "Cancelled"},
			],
			{"CS-001"},
		)

		self.assertEqual(len(states), 1)
		self.assertEqual(states[0]["teacher"], "TEA-OVERRIDE")
		self.assertEqual(states[0]["expected_student_count"], 2)
		self.assertTrue(states[0]["needs_attendance"])
		self.assertFalse(states[0]["needs_photos"])

	def test_leave_and_cancelled_only_means_no_expected_students(self):
		states = _build_completion_states(
			self.sessions,
			self.timeslots,
			[
				{"course_session": "CS-001", "status": "Leave"},
				{"course_session": "CS-001", "status": "Cancelled"},
			],
			set(),
		)
		self.assertEqual(states, [])

	def test_published_photo_and_completed_attendance_skip_email(self):
		states = _build_completion_states(
			self.sessions,
			self.timeslots,
			[
				{"course_session": "CS-001", "status": "Present"},
				{"course_session": "CS-001", "status": "Late"},
				{"course_session": "CS-001", "status": "Absent"},
			],
			{"CS-001"},
		)
		self.assertEqual(states, [])

	def test_draft_or_missing_photo_keeps_photo_action_pending(self):
		states = _build_completion_states(
			self.sessions,
			self.timeslots,
			[{"course_session": "CS-001", "status": "Present"}],
			set(),
		)
		self.assertEqual(len(states), 1)
		self.assertFalse(states[0]["needs_attendance"])
		self.assertTrue(states[0]["needs_photos"])

	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders.formatdate",
		return_value="Friday 17 July 2026",
	)
	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._",
		side_effect=lambda value: value,
	)
	def test_email_lists_only_missing_actions_and_uses_generic_portal(self, _mock_translate, _mock_formatdate):
		session = {
			"session_date": "2026-07-17",
			"course": "Anime <Art>",
			"campus": "Indooroopilly",
			"classroom": "Studio 1",
			"start_time": "16:00",
			"end_time": "17:30",
			"needs_attendance": True,
			"needs_photos": False,
		}
		message = _teacher_session_completion_message(session, teacher_name="Suki <Teacher>")

		self.assertIn("Suki &lt;Teacher&gt;", message)
		self.assertIn("Anime &lt;Art&gt;", message)
		self.assertIn("Attendance is not complete", message)
		self.assertNotIn("Class photos have not been published", message)
		self.assertIn(TEACHER_PORTAL_URL, message)
		self.assertNotIn("/sessions/", message)

	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders.formatdate",
		return_value="Friday 17 July 2026",
	)
	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._",
		side_effect=lambda value: value,
	)
	def test_email_can_list_both_actions(self, _mock_translate, _mock_formatdate):
		message = _teacher_session_completion_message(
			{
				"session_date": "2026-07-17",
				"course": "Creative Art",
				"campus": "Upper Mount Gravatt",
				"classroom": "Studio 2",
				"start_time": "15:45",
				"end_time": "16:45",
				"needs_attendance": True,
				"needs_photos": True,
			}
		)
		self.assertIn("Attendance is not complete", message)
		self.assertIn("Class photos have not been published", message)

	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._",
		side_effect=lambda value: value,
	)
	def test_subject_contains_course_and_end_time(self, _mock_translate):
		self.assertEqual(
			_teacher_session_completion_subject({"course": "Creative Art", "end_time": "16:45"}),
			"Action needed before class ends: Creative Art — 16:45",
		)

	def test_event_key_is_stable_short_and_unique_per_session(self):
		first = teacher_session_completion_event_key("CS-001")
		self.assertEqual(first, teacher_session_completion_event_key("CS-001"))
		self.assertNotEqual(first, teacher_session_completion_event_key("CS-002"))
		self.assertLessEqual(len(first), 140)

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.frappe.conf", {})
	def test_operational_switch_defaults_to_enabled(self):
		self.assertTrue(teacher_session_completion_reminder_enabled())

	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders.frappe.conf",
		{"qas_teacher_session_completion_reminder_enabled": 0},
	)
	def test_operational_switch_can_disable_reminders(self):
		self.assertFalse(teacher_session_completion_reminder_enabled())

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._queue_session_completion_reminder")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._get_due_session_completion_states")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.get_datetime_in_timezone")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.teacher_session_completion_reminder_enabled", return_value=True)
	def test_scheduler_uses_explicit_brisbane_time(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_timezone,
		mock_due,
		mock_queue,
	):
		now = datetime(2026, 7, 17, 17, 20)
		mock_timezone.return_value = now
		mock_due.return_value = []

		result = run_teacher_session_completion_reminders()

		mock_timezone.assert_called_once_with("Australia/Brisbane")
		mock_due.assert_called_once_with(now)
		mock_queue.assert_not_called()
		self.assertEqual(result["eligible"], 0)

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.frappe.enqueue")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._notification_event_exists", return_value=False)
	def test_queue_uses_deterministic_deduplicated_job_id(self, _mock_exists, mock_enqueue):
		self.assertEqual(_queue_session_completion_reminder({"name": "CS-001"}), "queued")
		kwargs = mock_enqueue.call_args.kwargs
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["course_session"], "CS-001")
		self.assertIn("teacher_session_completion-", kwargs["job_id"])

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._get_session_completion_state")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.teacher_session_completion_reminder_enabled", return_value=True)
	def test_worker_revalidates_and_skips_stale_session(self, _mock_enabled, _mock_outbound, mock_state):
		mock_state.return_value = None
		result = send_teacher_session_completion_reminder_job("CS-001", now=datetime(2026, 7, 17, 17, 20))
		self.assertTrue(result["skipped"])
		self.assertIn("no longer eligible", result["reason"])

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._notification_event_exists", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._get_session_completion_state")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.teacher_session_completion_reminder_enabled", return_value=True)
	def test_worker_skips_when_event_was_already_reserved(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_state,
		_mock_exists,
	):
		mock_state.return_value = {"name": "CS-001"}
		result = send_teacher_session_completion_reminder_job("CS-001", now=datetime(2026, 7, 17, 17, 20))
		self.assertTrue(result["skipped"])
		self.assertIn("already been recorded", result["reason"])

	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders.formatdate",
		return_value="Friday 17 July 2026",
	)
	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._",
		side_effect=lambda value: value,
	)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._mark_notification_sent")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._mark_notification_queued")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.sendmail_or_skip")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._create_notification_log", return_value="LOG-001")
	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._get_teacher_info",
		return_value={"name": "TEA-001", "teacher_name": "Suki", "email": "suki@example.com"},
	)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._get_session_completion_state")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.teacher_session_completion_reminder_enabled", return_value=True)
	def test_worker_reserves_event_then_sends_one_email(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_state,
		_mock_exists,
		_mock_teacher,
		mock_create_log,
		mock_sendmail,
		mock_queued,
		mock_sent,
		_mock_translate,
		_mock_formatdate,
	):
		mock_state.return_value = {
			"name": "CS-001",
			"session_date": "2026-07-17",
			"course": "Creative Art",
			"campus": "Indooroopilly",
			"classroom": "Studio 1",
			"start_time": "16:00",
			"end_time": "17:30",
			"teacher": "TEA-001",
			"needs_attendance": True,
			"needs_photos": True,
		}
		mock_sendmail.return_value = None

		result = send_teacher_session_completion_reminder_job(
			"CS-001",
			now=datetime(2026, 7, 17, 17, 20),
		)

		self.assertTrue(result["sent"])
		mock_create_log.assert_called_once()
		mock_queued.assert_called_once_with("LOG-001")
		mock_sent.assert_called_once_with("LOG-001")
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["suki@example.com"])
		self.assertIn(TEACHER_PORTAL_URL, mock_sendmail.call_args.kwargs["message"])

	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.frappe.log_error")
	@patch(
		"qas_custom.modules.notifications.teacher_session_completion_reminders._queue_session_completion_reminder",
		side_effect=[RuntimeError("first session failed"), "queued"],
	)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders._get_due_session_completion_states")
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.teacher_session_completion_reminders.teacher_session_completion_reminder_enabled", return_value=True)
	def test_one_session_failure_does_not_stop_other_sessions(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_due,
		_mock_queue,
		_mock_log_error,
	):
		mock_due.return_value = [{"name": "CS-001"}, {"name": "CS-002"}]
		result = run_teacher_session_completion_reminders(now=datetime(2026, 7, 17, 17, 20))
		self.assertEqual(result["failed"], 1)
		self.assertEqual(result["queued"], 1)
