from unittest import TestCase
from unittest.mock import patch

from frappe import _dict

from qas_custom.services.inquiry import _map_trial_form_session, _parse_class_language


class TestInquiryTrialLanguageMatching(TestCase):
	def _map_submission(self, class_session, timeslots=None):
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

		def get_all(doctype, filters=None, **kwargs):
			if doctype == "Weekly Timeslot":
				return [row for row in timeslots if row.class_language == filters.get("class_language")]
			if doctype == "Course Sessions":
				return [
					_dict(
						name="SESSION-001",
						weekly_timeslot=filters["weekly_timeslot"],
						session_date=filters["session_date"],
						status="Scheduled",
					)
				]
			raise AssertionError(f"Unexpected doctype: {doctype}")

		meta = _dict(has_field=lambda fieldname: fieldname == "status")
		with (
			patch("qas_custom.services.inquiry._derive_campus_and_course", return_value=("Upper Mount Gravatt", "Designer Art - Beginner")),
			patch("qas_custom.services.inquiry._resolve_campus", return_value="Upper Mount Gravatt"),
			patch("qas_custom.services.inquiry._resolve_course", return_value="Designer Art - Beginner"),
			patch("qas_custom.services.inquiry.frappe.get_meta", return_value=meta),
			patch("qas_custom.services.inquiry.frappe.get_all", side_effect=get_all) as mock_get_all,
			patch("qas_custom.services.inquiry._course_session_has_started", return_value=False),
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

	def test_duplicate_timeslots_in_requested_language_still_need_review(self):
		result, filters = self._map_submission(
			"Sat 10:40-12:10",
			timeslots=[
				_dict(name="WT-ENGLISH-1", class_language="English"),
				_dict(name="WT-ENGLISH-2", class_language="English"),
			],
		)

		self.assertEqual(filters["class_language"], "English")
		self.assertEqual(
			result["reason"],
			"Multiple Weekly Timeslots matched the submitted campus, weekday, and time.",
		)
		self.assertNotIn("course_session", result)

	def test_chinese_marker_is_case_insensitive(self):
		self.assertEqual(_parse_class_language("Sat 10:40-12:10 (IN CHINESE)"), "Chinese")
		self.assertEqual(_parse_class_language("Sat 10:40-12:10"), "English")
