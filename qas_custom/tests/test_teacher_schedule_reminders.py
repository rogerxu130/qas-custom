from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.attendance import commands as attendance_commands
from qas_custom.modules.notifications.teacher_schedule_reminders import (
	_build_schedule_groups,
	_teacher_schedule_message,
	_teacher_schedule_subject,
	teacher_next_day_schedule_event_key,
)


class TestTeacherScheduleReminders(TestCase):
	def test_present_late_parent_email_helper_is_removed(self):
		self.assertFalse(hasattr(attendance_commands, "notify_parent_if_present"))

	def test_groups_eligible_attendance_and_counts_trial_makeup(self):
		groups = _build_schedule_groups(
			[{"name": "CS-001", "weekly_timeslot": "WT-001", "teacher_override": "TEA-OVERRIDE"}],
			{"WT-001": {"teacher": "TEA-WEEKLY", "course": "Anime Art", "campus": "Indooroopilly", "start_time": "16:00:00", "end_time": "17:30:00"}},
			[
				{"course_session": "CS-001", "enrollment_type": "Full-Term", "status": "To be started"},
				{"course_session": "CS-001", "enrollment_type": "Trial", "status": "To be started"},
				{"course_session": "CS-001", "enrollment_type": "Makeup", "status": "To be started"},
				{"course_session": "CS-001", "enrollment_type": "Full-Term", "status": "Leave"},
				{"course_session": "CS-001", "enrollment_type": "Full-Term", "status": "Cancelled"},
			],
		)

		self.assertEqual(list(groups), ["TEA-OVERRIDE"])
		self.assertEqual(groups["TEA-OVERRIDE"][0]["student_count"], 3)
		self.assertEqual(groups["TEA-OVERRIDE"][0]["trial_count"], 1)
		self.assertEqual(groups["TEA-OVERRIDE"][0]["makeup_count"], 1)

	@patch("qas_custom.modules.notifications.teacher_schedule_reminders._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.teacher_schedule_reminders.formatdate", return_value="Thursday 16 July 2026")
	def test_message_uses_qas_cards_and_never_student_names(self, _mock_formatdate, _mock_translate):
		message = _teacher_schedule_message(
			"2026-07-16",
			[{"course": "Anime Art", "campus": "Indooroopilly", "start_time": "16:00", "end_time": "17:30", "student_count": 3, "trial_count": 1, "makeup_count": 1}],
			teacher_name="Suki",
		)

		self.assertIn("Hi Suki,", message)
		self.assertIn("Tomorrow&apos;s Classes", message)
		self.assertIn("Anime Art", message)
		self.assertIn("Indooroopilly", message)
		self.assertIn("16:00", message)
		self.assertIn("17:30", message)
		self.assertIn("3 students", message)
		self.assertIn("1 Trial", message)
		self.assertIn("1 Makeup", message)
		self.assertIn("Queensland Art School", message)
		self.assertIn("background-color:#172033", message)
		self.assertNotIn("Ava", message)

	@patch("qas_custom.modules.notifications.teacher_schedule_reminders._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.teacher_schedule_reminders.formatdate", return_value="Friday 17 July 2026")
	def test_message_hides_zero_badges_and_uses_fallback_greeting(self, _mock_formatdate, _mock_translate):
		message = _teacher_schedule_message(
			"2026-07-17",
			[{"course": "Creative Art", "campus": "Upper Mount Gravatt", "start_time": "15:45", "end_time": "16:45", "student_count": 1, "trial_count": 0, "makeup_count": 0}],
		)

		self.assertIn("Hello,", message)
		self.assertIn("You have 1 class.", message)
		self.assertIn("1 student", message)
		self.assertNotIn("Trial", message)
		self.assertNotIn("Makeup", message)

	@patch("qas_custom.modules.notifications.teacher_schedule_reminders._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.teacher_schedule_reminders.formatdate", return_value="Friday 17 July 2026")
	def test_message_escapes_teacher_and_session_values(self, _mock_formatdate, _mock_translate):
		message = _teacher_schedule_message(
			"2026-07-17",
			[{"course": "Art <script>", "campus": "A & B", "start_time": "15:45", "end_time": "16:45", "student_count": 2, "trial_count": 0, "makeup_count": 0}],
			teacher_name="Suki <Admin>",
		)

		self.assertIn("Suki &lt;Admin&gt;", message)
		self.assertIn("Art &lt;script&gt;", message)
		self.assertIn("A &amp; B", message)
		self.assertNotIn("<script>", message)

	@patch("qas_custom.modules.notifications.teacher_schedule_reminders._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.teacher_schedule_reminders.formatdate", return_value="Friday 17 July 2026")
	def test_subject_is_tomorrow_classes_with_target_date(self, _mock_formatdate, _mock_translate):
		self.assertEqual(
			_teacher_schedule_subject("2026-07-17"),
			"Tomorrow's classes — Friday 17 July 2026",
		)

	def test_event_key_is_stable_per_teacher_and_target_date(self):
		self.assertEqual(
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
		)
		self.assertNotEqual(
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-17"),
		)
