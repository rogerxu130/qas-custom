from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.attendance import commands as attendance_commands
from qas_custom.modules.notifications.teacher_schedule_reminders import (
	_build_schedule_groups,
	_teacher_schedule_message,
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
	def test_message_has_counts_but_never_student_names(self, _mock_formatdate, _mock_translate):
		message = _teacher_schedule_message(
			"2026-07-16",
			[{"course": "Anime Art", "campus": "Indooroopilly", "start_time": "16:00", "end_time": "17:30", "student_count": 3, "trial_count": 1, "makeup_count": 1}],
		)

		self.assertIn("Students", message)
		self.assertIn("Trial", message)
		self.assertIn("Makeup", message)
		self.assertIn("3", message)
		self.assertNotIn("Ava", message)

	def test_event_key_is_stable_per_teacher_and_target_date(self):
		self.assertEqual(
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
		)
		self.assertNotEqual(
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-16"),
			teacher_next_day_schedule_event_key("TEA-001", "2026-07-17"),
		)
