from unittest import TestCase
from unittest.mock import patch

from qas_custom.api.campus_admin import campus_admin_get_current_term_enrollments
from qas_custom.services.campus_admin import get_campus_admin_current_term_enrollments_data


class TestCampusAdminCurrentTermEnrollmentSearch(TestCase):
	@patch("qas_custom.services.campus_admin.frappe.get_all")
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly"]},
	)
	def test_returns_current_term_enrollments_only_from_assigned_campuses(self, _profile, get_all):
		def get_all_side_effect(doctype, **kwargs):
			if doctype == "Term":
				return [{"name": "Term 3 2026"}]
			if doctype == "Student":
				return [
					{"name": "STU-AMY", "student_name": "Amy Chen"},
					{"name": "STU-OTHER", "student_name": "Amy Other"},
				]
			if doctype == "Weekly Timeslot":
				self.assertEqual(kwargs["filters"], {"term": "Term 3 2026", "campus": ["in", ["Indooroopilly"]]})
				return [
					{
						"name": "TS-INDOOR",
						"course": "Anime Art - Intermediate",
						"campus": "Indooroopilly",
						"day_of_week": "Monday",
						"start_time": "16:50:00",
						"end_time": "18:20:00",
					}
				]
			if doctype == "Enrollment":
				self.assertEqual(kwargs["filters"]["term"], "Term 3 2026")
				self.assertEqual(kwargs["filters"]["weekly_timeslot"], ["in", ["TS-INDOOR"]])
				return [
					{
						"name": "ENR-PLANNED",
						"student": "STU-AMY",
						"term": "Term 3 2026",
						"course": "Anime Art - Intermediate",
						"weekly_timeslot": "TS-INDOOR",
						"enrollment_type": "Full-Term",
						"status": "Planned",
					},
					{
						"name": "ENR-CANCELLED",
						"student": "STU-AMY",
						"term": "Term 3 2026",
						"course": "Anime Art - Intermediate",
						"weekly_timeslot": "TS-INDOOR",
						"enrollment_type": "Full-Term",
						"status": "Cancelled",
					},
				]
			self.fail(f"Unexpected doctype: {doctype}")

		get_all.side_effect = get_all_side_effect

		result = get_campus_admin_current_term_enrollments_data(query="Amy")

		self.assertEqual(result["term"], "Term 3 2026")
		self.assertTrue(result["searched"])
		self.assertEqual(result["state"], "ready")
		self.assertEqual([item["name"] for item in result["items"]], ["ENR-CANCELLED", "ENR-PLANNED"])
		self.assertEqual({item["status"] for item in result["items"]}, {"Planned", "Cancelled"})
		self.assertTrue(all(item["campus"] == "Indooroopilly" for item in result["items"]))
		self.assertTrue(all("invoice" not in item for item in result["items"]))

	@patch("qas_custom.services.campus_admin.frappe.get_all", return_value=[{"name": "Term 3 2026"}])
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly"]},
	)
	def test_blank_query_does_not_query_students_timeslots_or_enrollments(self, _profile, get_all):
		result = get_campus_admin_current_term_enrollments_data(query="  ")

		self.assertEqual(result, {"items": [], "term": "Term 3 2026", "searched": False, "state": "ready"})
		self.assertEqual([call.args[0] for call in get_all.call_args_list], ["Term"])

	@patch("qas_custom.services.campus_admin.frappe.get_all", return_value=[])
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly"]},
	)
	def test_no_active_term_is_an_explicit_empty_state(self, _profile, _get_all):
		result = get_campus_admin_current_term_enrollments_data(query="Amy")

		self.assertEqual(result["state"], "no_active_term")
		self.assertFalse(result["searched"])
		self.assertEqual(result["items"], [])

	@patch(
		"qas_custom.services.campus_admin.frappe.get_all",
		return_value=[{"name": "Term 3 2026"}, {"name": "Term 4 2026"}],
	)
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly"]},
	)
	def test_multiple_active_terms_is_an_explicit_configuration_state(self, _profile, _get_all):
		result = get_campus_admin_current_term_enrollments_data(query="Amy")

		self.assertEqual(result["state"], "multiple_active_terms")
		self.assertIn("Multiple active terms", result["message"])
		self.assertEqual(result["items"], [])

	@patch("qas_custom.api.campus_admin.get_campus_admin_current_term_enrollments_data")
	def test_api_forwards_search_arguments(self, search_enrollments):
		search_enrollments.return_value = {"items": []}

		result = campus_admin_get_current_term_enrollments.__wrapped__(query="Amy", limit=25)

		self.assertEqual(result, {"items": []})
		search_enrollments.assert_called_once_with(query="Amy", limit=25)
