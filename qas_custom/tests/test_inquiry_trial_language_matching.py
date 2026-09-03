from unittest import TestCase
from unittest.mock import patch

from frappe import _dict

from qas_custom.services.inquiry import _map_trial_form_session, _parse_class_language


class TestInquiryTrialLanguageMatching(TestCase):
	def _map_submission(self, class_session, timeslots=None, sessions=None, started_session_names=None):
		timeslots = timeslots or [
			_dict(
				name="WT-ENGLISH",
				course="Designer Art - Beginner",
				class_language="English",
				campus="Upper Mount Gravatt",
				start_time="10:40:00",
				end_time="12:10:00",
				status="Active",
			)
		]
		if sessions is None:
			class_language = _parse_class_language(class_session)
			matched_timeslot = next(row for row in timeslots if row.class_language == class_language)
			sessions = [
				_dict(
					name="SESSION-001",
					weekly_timeslot=matched_timeslot.name,
					session_date="2026-09-05",
					status="Scheduled",
				)
			]
		started_session_names = set(started_session_names or [])

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Weekly Timeslot":
				return [row for row in timeslots if row.class_language == filters.get("class_language")]
			if doctype == "Course Sessions":
				candidate_timeslots = filters["weekly_timeslot"][1]
				return [
					row
					for row in sessions
					if row.weekly_timeslot in candidate_timeslots and row.session_date == filters["session_date"]
				]
			raise AssertionError(f"Unexpected doctype: {doctype}")

		meta = _dict(has_field=lambda fieldname: fieldname == "status")
		with (
			patch("qas_custom.services.inquiry._derive_campus_and_course", return_value=("Upper Mount Gravatt", "Designer Art - Beginner")),
			patch("qas_custom.services.inquiry._resolve_campus", return_value="Upper Mount Gravatt"),
			patch("qas_custom.services.inquiry._resolve_course", return_value="Designer Art - Beginner"),
			patch("qas_custom.services.inquiry.frappe.get_meta", return_value=meta),
			patch("qas_custom.services.inquiry.frappe.get_all", side_effect=get_all) as mock_get_all,
			patch(
				"qas_custom.services.inquiry._course_session_has_started",
				side_effect=lambda session, _timeslot: session.name in started_session_names,
			),
			patch("qas_custom.services.inquiry._", side_effect=lambda value: value),
		):
			result = _map_trial_form_session(
				{
					"submitted_form_name": "Upper Mount Gravatt Designer Art - Beginner",
					"submitted_class_session": class_session,
					"submitted_trial_date": "2026-09-05",
				}
			)

		return result, mock_get_all.call_args_list[0].kwargs["filters"]

	def test_plain_session_matches_english_timeslot(self):
		result, filters = self._map_submission(
			"Sat 10:40-12:10",
			timeslots=[
				_dict(name="WT-ENGLISH", class_language="English"),
				_dict(name="WT-CHINESE", class_language="Chinese"),
			],
		)

		self.assertEqual(filters["class_language"], "English")
		self.assertEqual(result["course_session"], "SESSION-001")

	def test_chinese_marker_matches_chinese_timeslot(self):
		result, filters = self._map_submission(
			"Sat 10:40-12:10 (in Chinese)",
			timeslots=[
				_dict(name="WT-ENGLISH", class_language="English"),
				_dict(name="WT-CHINESE", class_language="Chinese"),
			],
		)

		self.assertEqual(filters["class_language"], "Chinese")
		self.assertEqual(result["course_session"], "SESSION-001")

	def test_duplicate_timeslots_book_the_only_session_on_requested_date(self):
		result, filters = self._map_submission(
			"Sat 10:40-12:10",
			timeslots=[
				_dict(name="WT-TERM-3", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
				_dict(name="WT-TERM-4", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
			],
			sessions=[
				_dict(name="SESSION-TERM-3", weekly_timeslot="WT-TERM-3", session_date="2026-06-20", status="Scheduled"),
				_dict(name="SESSION-TERM-4", weekly_timeslot="WT-TERM-4", session_date="2026-09-05", status="Scheduled"),
			],
		)

		self.assertEqual(filters["class_language"], "English")
		self.assertEqual(result["course_session"], "SESSION-TERM-4")
		self.assertNotIn("reason", result)

	def test_cancelled_session_does_not_hide_only_bookable_session(self):
		result, _filters = self._map_submission(
			"Sat 10:40-12:10",
			timeslots=[
				_dict(name="WT-TERM-3", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
				_dict(name="WT-TERM-4", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
			],
			sessions=[
				_dict(name="SESSION-CANCELLED", weekly_timeslot="WT-TERM-3", session_date="2026-09-05", status="Cancelled"),
				_dict(name="SESSION-BOOKABLE", weekly_timeslot="WT-TERM-4", session_date="2026-09-05", status="Scheduled"),
			],
		)

		self.assertEqual(result["course_session"], "SESSION-BOOKABLE")
		self.assertNotIn("reason", result)

	def test_multiple_bookable_sessions_on_requested_date_need_review(self):
		result, _filters = self._map_submission(
			"Sat 10:40-12:10",
			timeslots=[
				_dict(name="WT-TERM-3", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
				_dict(name="WT-TERM-4", course="Designer Art - Beginner", class_language="English", campus="Upper Mount Gravatt", start_time="10:40:00", end_time="12:10:00", status="Active"),
			],
			sessions=[
				_dict(name="SESSION-TERM-3", weekly_timeslot="WT-TERM-3", session_date="2026-09-05", status="Scheduled"),
				_dict(name="SESSION-TERM-4", weekly_timeslot="WT-TERM-4", session_date="2026-09-05", status="Scheduled"),
			],
		)

		self.assertEqual(
			result["reason"],
			"Multiple Course Sessions matched the submitted trial request.",
		)
		self.assertNotIn("course_session", result)

	def test_no_session_on_requested_date_needs_review(self):
		result, _filters = self._map_submission("Sat 10:40-12:10", sessions=[])

		self.assertEqual(result["reason"], "No Course Session matched the submitted trial date and schedule.")
		self.assertNotIn("course_session", result)

	def test_only_cancelled_session_needs_review(self):
		result, _filters = self._map_submission(
			"Sat 10:40-12:10",
			sessions=[
				_dict(name="SESSION-CANCELLED", weekly_timeslot="WT-ENGLISH", session_date="2026-09-05", status="Cancelled"),
			],
		)

		self.assertEqual(result["reason"], "Matched Course Session is cancelled.")
		self.assertNotIn("course_session", result)

	def test_only_started_session_needs_review(self):
		result, _filters = self._map_submission(
			"Sat 10:40-12:10",
			sessions=[
				_dict(name="SESSION-STARTED", weekly_timeslot="WT-ENGLISH", session_date="2026-09-05", status="Scheduled"),
			],
			started_session_names={"SESSION-STARTED"},
		)

		self.assertEqual(result["reason"], "Matched Course Session has already started.")
		self.assertNotIn("course_session", result)

	def test_chinese_marker_is_case_insensitive(self):
		self.assertEqual(_parse_class_language("Sat 10:40-12:10 (IN CHINESE)"), "Chinese")
		self.assertEqual(_parse_class_language("Sat 10:40-12:10"), "English")
