from unittest import TestCase

from qas_custom.services.school_admin_reporting import (
	_build_daily_teacher_report_items,
	_daily_teacher_report_summary,
)


class TestSchoolAdminDailyTeacherReport(TestCase):
	def test_builds_session_owned_report_with_attendance_updates_and_private_media_urls(self):
		items = _build_daily_teacher_report_items(
			sessions=[
				{"name": "CS-LATE", "weekly_timeslot": "WT-LATE", "teacher_override": ""},
				{"name": "CS-EARLY", "weekly_timeslot": "WT-EARLY", "teacher_override": "TEA-OVERRIDE"},
			],
			timeslots={
				"WT-LATE": {
					"course": "Anime Art", "campus": "Indooroopilly", "classroom": "R2",
					"teacher": "TEA-WEEKLY", "start_time": "16:00:00", "end_time": "17:30:00",
				},
				"WT-EARLY": {
					"course": "Designer Art", "campus": "Upper Mount Gravatt", "classroom": "R1",
					"teacher": "TEA-WEEKLY", "start_time": "09:00:00", "end_time": "10:30:00",
				},
			},
			attendance_rows=[
				{"course_session": "CS-EARLY", "status": "Present"},
				{"course_session": "CS-EARLY", "status": "Late"},
				{"course_session": "CS-EARLY", "status": "To be started"},
				{"course_session": "CS-EARLY", "status": "Leave"},
				{"course_session": "CS-EARLY", "status": "Cancelled"},
				{"course_session": "CS-LATE", "status": "Absent"},
			],
			updates=[
				{
					"name": "UPD-1", "course_session": "CS-EARLY", "title": "Colour studies",
					"description": "Students practised colour mixing.", "teacher": "TEA-OVERRIDE",
				}
			],
			photo_posts=[
				{
					"name": "PHOTO-1", "course_session": "CS-EARLY", "title": "Class work",
					"caption": "Finished portraits", "teacher": "TEA-OVERRIDE",
				}
			],
			photo_items=[{"parent": "PHOTO-1", "idx": 1}, {"parent": "PHOTO-1", "idx": 2}],
			videos=[{"name": "VIDEO-1", "course_session": "CS-EARLY", "teacher": "TEA-OVERRIDE"}],
			teacher_names={"TEA-WEEKLY": "Weekly Teacher", "TEA-OVERRIDE": "Override Teacher"},
		)

		self.assertEqual([row["course_session"] for row in items], ["CS-EARLY", "CS-LATE"])
		early = items[0]
		self.assertEqual(early["teacher_name"], "Override Teacher")
		self.assertEqual(early["attendance"], {
			"expected": 3, "marked": 2, "unmarked": 1, "present": 1, "absent": 0,
			"late": 1, "leave": 1, "cancelled": 1,
		})
		self.assertEqual(early["class_updates"][0]["description"], "Students practised colour mixing.")
		self.assertEqual(early["photo_posts"][0]["caption"], "Finished portraits")
		self.assertEqual(early["photo_count"], 2)
		self.assertEqual(early["video_count"], 1)
		self.assertIn("school_admin_get_course_session_photo_preview", early["photos"][0]["preview_url"])
		self.assertIn("school_admin_get_course_session_photo", early["photos"][0]["full_url"])
		self.assertFalse(early["needs_attention"]["class_update"])
		self.assertFalse(early["needs_attention"]["media"])
		self.assertTrue(items[1]["needs_attention"]["class_update"])
		self.assertTrue(items[1]["needs_attention"]["media"])

	def test_summary_counts_sessions_that_need_follow_up(self):
		summary = _daily_teacher_report_summary([
			{"photo_count": 2, "needs_attention": {"attendance": True, "class_update": False, "media": False}},
			{"photo_count": 0, "needs_attention": {"attendance": False, "class_update": True, "media": True}},
		])
		self.assertEqual(summary, {
			"session_count": 2,
			"incomplete_attendance_count": 1,
			"missing_class_update_count": 1,
			"missing_media_count": 1,
			"photo_count": 2,
		})
