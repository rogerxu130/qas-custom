import csv
from datetime import time, timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zipfile import ZipFile

from qas_custom.services import school_admin_timetable_export as export


class TestTimetableExport(TestCase):
	def setUp(self):
		self.course = dict(name="COURSE-001", course_name='Art, "Creative"', course_name_zh="创意美术",
			status="Active", min_age=3.5, max_age=5, duration_mins=90, total_session_per_term=9,
			trial_fee=0, full_term_fee=500)
		self.teacher = dict(name="TEACHER-001", teacher_name="张老师, Alice", status="Active")
		self.room = dict(name="indooroopilly-r1", classroom_name="R1", campus="Indooroopilly")
		self.campus = dict(name="Indooroopilly", campus_name="Indooroopilly")
		self.session = dict(name="WEEKLY-001", course=self.course["name"], teacher=self.teacher["name"],
			classroom=self.room["name"], campus=self.campus["name"], day_of_week="Monday",
			start_time=timedelta(hours=9, minutes=30), class_language="Chinese")

	def build(self, **overrides):
		args = dict(sessions=[self.session], courses=[self.course], teachers=[self.teacher],
			rooms=[self.room], campuses=[self.campus], trial_field="trial_fee")
		args.update(overrides)
		return export.build_timetable_zip(**args)

	def rows(self, data, name):
		with ZipFile(BytesIO(data)) as archive:
			raw = archive.read(name)
			self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
			return list(csv.DictReader(StringIO(raw.decode("utf-8-sig"))))

	def test_three_files_match_scheduler_contract(self):
		data = self.build()
		with ZipFile(BytesIO(data)) as archive:
			self.assertEqual(archive.namelist(), ["qas_course_true_meta.csv", "qas_session_table.csv", "qas_teachers.csv"])
		courses = self.rows(data, "qas_course_true_meta.csv")
		self.assertEqual(tuple(courses[0]), export.COURSE_HEADERS)
		self.assertEqual(courses[0]["course_name_en"], 'Art, "Creative"')
		self.assertEqual(courses[0]["course_name_zh"], "创意美术")
		self.assertEqual(courses[0]["suitable_age_zh"], "3.5-5岁")
		self.assertEqual(courses[0]["session_length_hours"], "1.5")
		self.assertEqual(courses[0]["total_hours_per_term"], "13.5")
		self.assertEqual(courses[0]["trial_price_aud"], "0")
		sessions = self.rows(data, "qas_session_table.csv")
		self.assertEqual(tuple(sessions[0]), export.SESSION_HEADERS)
		self.assertEqual(sessions[0], dict(course_id="COURSE-001", language="Chinese",
			teacher_name="张老师, Alice", room="R1", start_time="09:30", campus="indooroopilly", weekday="mon", planned_active_student_count="0"))
		self.assertEqual(self.rows(data, "qas_teachers.csv"), [{"teacher_name": "张老师, Alice"}])

	def test_unassigned_and_referenced_inactive_records_are_preserved(self):
		self.course["status"] = "Inactive"
		self.teacher["status"] = "Inactive"
		sessions = [self.session, dict(self.session, teacher="", name="WEEKLY-002")]
		data = self.build(sessions=sessions,
			courses=[self.course, dict(self.course, name="UNUSED", status="Inactive")],
			teachers=[self.teacher, dict(self.teacher, name="UNUSED", teacher_name="Unused", status="Inactive")])
		self.assertEqual(len(self.rows(data, "qas_course_true_meta.csv")), 1)
		self.assertEqual(len(self.rows(data, "qas_teachers.csv")), 1)
		self.assertEqual([row["teacher_name"] for row in self.rows(data, "qas_session_table.csv")], ["张老师, Alice", ""])

	def test_active_catalogue_and_deduplicated_teacher_names(self):
		data = self.build(courses=[self.course, dict(self.course, name="ADDITIONAL")],
			teachers=[self.teacher, dict(self.teacher, name="DUPLICATE"),
				dict(self.teacher, name="ANOTHER", teacher_name="Bob")])
		self.assertEqual(len(self.rows(data, "qas_course_true_meta.csv")), 2)
		self.assertEqual(len(self.rows(data, "qas_teachers.csv")), 2)

	def test_makeup_courses_without_duration_are_excluded(self):
		for flag in (1, "1"):
			with self.subTest(flag=flag):
				makeup = dict(self.course, name="MAKEUP", is_makeup_course=flag, duration_mins=0)
				data = self.build(courses=[self.course, makeup])
				self.assertEqual([r["course_id"] for r in self.rows(data, "qas_course_true_meta.csv")], [self.course["name"]])

	def test_makeup_weekly_classes_are_excluded_before_validation(self):
		makeup = dict(self.course, name="MAKEUP", is_makeup_course=1, duration_mins=None)
		makeup_session = dict(self.session, course="MAKEUP", classroom="missing", teacher="missing")
		data = self.build(courses=[self.course, makeup], sessions=[self.session, makeup_session])
		self.assertEqual(len(self.rows(data, "qas_session_table.csv")), 1)
		self.assertEqual(len(self.rows(data, "qas_course_true_meta.csv")), 1)
		with self.assertRaisesRegex(ValueError, "after excluding dedicated makeup courses"):
			self.build(courses=[makeup], sessions=[makeup_session])

	def test_unchecked_makeup_flag_keeps_regular_courses(self):
		for flag in (0, "0", None):
			with self.subTest(flag=flag):
				data = self.build(courses=[dict(self.course, is_makeup_course=flag)])
				self.assertEqual(len(self.rows(data, "qas_course_true_meta.csv")), 1)

	def test_student_counts_follow_weekly_class_and_default_to_zero(self):
		data = self.build(sessions=[self.session, dict(self.session, name="WEEKLY-002"),
			dict(self.session, name="WEEKLY-003")],
			student_counts={self.session["name"]: 5, "WEEKLY-002": 2, "OTHER": 99})
		rows = self.rows(data, "qas_session_table.csv")
		self.assertEqual([row["planned_active_student_count"] for row in rows], ["5", "2", "0"])
		self.assertEqual(tuple(rows[0]), ("course_id", "language", "teacher_name", "room", "start_time", "campus", "weekday", "planned_active_student_count"))

	def test_large_export_and_no_deduplication_of_classes(self):
		data = self.build(sessions=[dict(self.session, name=f"WEEKLY-{i}") for i in range(650)])
		self.assertEqual(len(self.rows(data, "qas_session_table.csv")), 650)

	def test_supported_campus_and_sunday_mapping(self):
		self.campus.update(name="UMG", campus_name="Upper Mount Gravatt")
		self.room["campus"] = "UMG"
		self.session.update(campus="UMG", day_of_week="Sunday", start_time=time(14, 5))
		row = self.rows(self.build(), "qas_session_table.csv")[0]
		self.assertEqual((row["campus"], row["weekday"], row["start_time"]), ("upper_mount_gravatt", "sun", "14:05"))

	def test_invalid_references_fail_instead_of_dropping_rows(self):
		for field in ("course", "teacher", "classroom", "campus"):
			with self.subTest(field=field), self.assertRaisesRegex(ValueError, "WEEKLY-001: missing"):
				self.build(sessions=[dict(self.session, **{field: "missing"})])

	def test_unknown_campus_and_bad_room_fail(self):
		with self.assertRaisesRegex(ValueError, "not supported"):
			self.build(campuses=[dict(self.campus, campus_name="New Campus")])
		with self.assertRaisesRegex(ValueError, "classroom name and campus"):
			self.build(rooms=[dict(self.room, campus="Other")])

	def test_room_dropdown_compatibility(self):
		for name in ("R1", "r1", "Room 1"):
			data = self.build(rooms=[dict(self.room, classroom_name=name)])
			self.assertEqual(self.rows(data, "qas_session_table.csv")[0]["room"], "R1")
		with self.assertRaisesRegex(ValueError, "R1–R5"):
			self.build(rooms=[dict(self.room, classroom_name="R6")])

	def test_time_formats_and_invalid_values(self):
		for value in ("9:30", "09:30:00", "9:30:00", time(9, 30), timedelta(hours=9, minutes=30)):
			self.assertEqual(export._start_time(value, "class"), "09:30")
		for value in (None, "24:00", "09:30:01", timedelta(days=1), timedelta(seconds=-60)):
			with self.subTest(value=value), self.assertRaises(ValueError):
				export._start_time(value, "class")

	def test_invalid_course_duration_and_language_fail(self):
		with self.assertRaisesRegex(ValueError, "positive lesson duration"):
			self.build(courses=[dict(self.course, duration_mins=0)])
		with self.assertRaisesRegex(ValueError, "language"):
			self.build(sessions=[dict(self.session, class_language="Other")])
		with self.assertRaisesRegex(ValueError, "weekday"):
			self.build(sessions=[dict(self.session, day_of_week="Other")])

	def fake_frappe(self, roles=("School Admin",), user="admin@example.test"):
		def throw(message, exception=ValueError):
			raise exception(message)
		return SimpleNamespace(session=SimpleNamespace(user=user), get_roles=Mock(return_value=roles),
			PermissionError=PermissionError, throw=throw, db=Mock(), get_all=Mock(),
			local=SimpleNamespace(response=SimpleNamespace()))

	def test_access_checked_before_data_queries(self):
		for roles, user in [((), "user"), (("Campus Admin",), "user"), (("School Admin",), "Guest")]:
			fake = self.fake_frappe(roles, user)
			with patch.object(export, "frappe", fake), patch.object(export, "_", lambda s: s):
				with self.assertRaises(PermissionError):
					export.export_school_admin_timetable_data("term")
			fake.get_all.assert_not_called()

	def test_unknown_and_empty_term_fail(self):
		fake = self.fake_frappe()
		fake.db.exists.return_value = False
		with patch.object(export, "frappe", fake), patch.object(export, "_", lambda s: s):
			with self.assertRaisesRegex(ValueError, "existing Term"):
				export.export_school_admin_timetable_data("missing")
			fake.db.exists.return_value = True
			fake.get_all.return_value = []
			with self.assertRaisesRegex(ValueError, "no active weekly classes"):
				export.export_school_admin_timetable_data("term")

	def test_endpoint_queries_all_rows_for_only_selected_term(self):
		fake = self.fake_frappe()
		fake.db.exists.return_value = True
		fake.db.get_value.return_value = "2026 Term 4"
		records = {"Weekly Timeslot": [self.session], "Course": [self.course], "Teacher": [self.teacher],
			"Classroom": [self.room], "Campus": [self.campus],
			"Enrollment": [{"weekly_timeslot": self.session["name"], "student_count": 5}]}
		fake.get_all.side_effect = lambda doctype, **kwargs: records[doctype]
		with patch.object(export, "frappe", fake), patch.object(export, "_", lambda s: s), \
			patch("qas_custom.modules.billing.commands.get_trial_class_fee_field", return_value="trial_fee"):
			export.export_school_admin_timetable_data("TERM-4")
		calls = fake.get_all.call_args_list
		self.assertIn("is_makeup_course", calls[1].kwargs["fields"])
		self.assertEqual(calls[0].kwargs["filters"], {"term": "TERM-4", "status": "Active"})
		self.assertTrue(all(call.kwargs["limit_page_length"] == 0 for call in calls))
		enrollment_query = next(call for call in calls if call.args[0] == "Enrollment")
		self.assertEqual(enrollment_query.kwargs["filters"], {
			"term": "TERM-4", "weekly_timeslot": ["in", [self.session["name"]]],
			"status": ["in", ["Planned", "Active"]],
		})
		self.assertEqual(enrollment_query.kwargs["fields"], ["weekly_timeslot", "count(distinct student) as student_count"])
		self.assertEqual(enrollment_query.kwargs["group_by"], "weekly_timeslot")
		self.assertEqual(self.rows(fake.local.response.filecontent, "qas_session_table.csv")[0]["planned_active_student_count"], "5")
		self.assertEqual(fake.local.response.filename, "2026_Term_4_timetable.zip")
		self.assertEqual(fake.local.response.content_type, "application/zip")
		self.assertEqual(len(self.rows(fake.local.response.filecontent, "qas_session_table.csv")), 1)
