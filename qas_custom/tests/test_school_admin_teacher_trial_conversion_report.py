from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin_reporting import _build_teacher_trial_conversion_items


class TestSchoolAdminTeacherTrialConversionReport(TestCase):
	@patch("qas_custom.services.school_admin_reporting.now_datetime", return_value=datetime(2026, 8, 1, 12, 0))
	def test_counts_only_actual_trial_attendees_by_historical_teacher_and_outcome(self, _mock_now):
		items = _build_teacher_trial_conversion_items(
			attendance_rows=[
				{"name": "ATT-1", "course_session": "CS-1", "source_document": "INQ-CONVERTED", "status": "Present"},
				{"name": "ATT-2", "course_session": "CS-2", "source_document": "INQ-FOLLOW-UP", "status": "Late"},
				{"name": "ATT-3", "course_session": "CS-3", "source_document": "INQ-INACTIVE", "status": "Present"},
				{"name": "ATT-4", "course_session": "CS-1", "source_document": "INQ-COMPLETED", "status": "Present"},
				{"name": "ATT-5", "course_session": "CS-4", "source_document": "INQ-UNMARKED-PAST", "status": "To be started"},
				{"name": "ATT-6", "course_session": "CS-5", "source_document": "INQ-UNMARKED-FUTURE", "status": "To be started"},
				{"name": "ATT-7", "course_session": "CS-1", "source_document": "INQ-NO-SHOW", "status": "To be started"},
				{"name": "ATT-8", "course_session": "CS-2", "source_document": "INQ-CONVERTED", "status": "Present"},
			],
			session_map={
				"CS-1": {"weekly_timeslot": "WT-1", "teacher_override": "", "session_date": "2026-07-20"},
				"CS-2": {"weekly_timeslot": "WT-1", "teacher_override": "TEA-OVERRIDE", "session_date": "2026-07-21"},
				"CS-3": {"weekly_timeslot": "WT-2", "teacher_override": "", "session_date": "2026-07-22"},
				"CS-4": {"weekly_timeslot": "WT-1", "teacher_override": "", "session_date": "2026-08-01"},
				"CS-5": {"weekly_timeslot": "WT-3", "teacher_override": "", "session_date": "2026-08-01"},
			},
			timeslot_map={
				"WT-1": {"teacher": "TEA-WEEKLY", "end_time": "11:00:00"},
				"WT-2": {"teacher": ""},
				"WT-3": {"teacher": "TEA-WEEKLY", "end_time": "20:00:00"},
			},
			inquiries={
				"INQ-CONVERTED": {"status": "Converted"},
				"INQ-FOLLOW-UP": {"status": "Follow-up"},
				"INQ-INACTIVE": {"status": "Inactive"},
				"INQ-COMPLETED": {"status": "Completed"},
				"INQ-UNMARKED-PAST": {"status": "Completed"},
				"INQ-UNMARKED-FUTURE": {"status": "Completed"},
				"INQ-NO-SHOW": {"status": "No-show"},
			},
			teacher_names={"TEA-WEEKLY": "Weekly Teacher", "TEA-OVERRIDE": "Override Teacher"},
		)

		self.assertEqual(
			items,
			[
				{
					"teacher": "TEA-WEEKLY",
					"teacher_name": "Weekly Teacher",
					"trial_attended_count": 3,
					"converted_count": 1,
					"inactive_count": 0,
					"following_up_count": 2,
				},
				{
					"teacher": "",
					"teacher_name": "No teacher assigned",
					"trial_attended_count": 1,
					"converted_count": 0,
					"inactive_count": 1,
					"following_up_count": 0,
				},
				{
					"teacher": "TEA-OVERRIDE",
					"teacher_name": "Override Teacher",
					"trial_attended_count": 1,
					"converted_count": 0,
					"inactive_count": 0,
					"following_up_count": 1,
				},
			],
		)
