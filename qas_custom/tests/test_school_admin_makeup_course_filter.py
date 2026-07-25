from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.api.school_admin import school_admin_get_redeemable_sessions
from qas_custom.services.school_admin import (
	_school_admin_makeup_course_options,
	get_school_admin_redeemable_sessions_data,
)


class FakeVoucher:
	name = "MV-001"

	def get(self, key, default=None):
		return {"course": "Anime"}.get(key, default)


def session(session_id, course):
	return {
		"session_id": session_id,
		"course": course,
		"session_date": "2026-08-01",
	}


class TestSchoolAdminMakeupCourseFilter(TestCase):
	@patch("qas_custom.services.school_admin._course_label_map")
	def test_original_course_is_first_even_without_sessions(self, label_map):
		label_map.return_value = {
			"Anime": {"course_name": "Anime Art", "course_name_zh": "动漫"},
			"Design": {"course_name": "Design", "course_name_zh": "设计"},
		}

		result = _school_admin_makeup_course_options(
			[session("CS-002", "Design")],
			"Anime",
		)

		self.assertEqual([row["course"] for row in result], ["Anime", "Design"])
		self.assertTrue(result[0]["is_original"])
		self.assertEqual(result[0]["session_count"], 0)
		self.assertEqual(result[1]["session_count"], 1)

	@patch("qas_custom.services.school_admin._course_label_map")
	def test_other_courses_are_sorted_by_display_label(self, label_map):
		label_map.return_value = {
			"Anime": {"course_name": "Anime"},
			"Painting": {"course_name": "Watercolour"},
			"Design": {"course_name": "Design"},
		}

		result = _school_admin_makeup_course_options(
			[
				session("CS-003", "Painting"),
				session("CS-002", "Design"),
				session("CS-001", "Design"),
			],
			"Anime",
		)

		self.assertEqual([row["course"] for row in result], ["Anime", "Design", "Painting"])
		self.assertEqual(result[1]["session_count"], 2)

	@patch("qas_custom.services.school_admin.preview_makeup_target_pricing", return_value={"classification": "same_course"})
	@patch("qas_custom.services.school_admin._course_label_map")
	@patch("qas_custom.services.school_admin.get_parent_redeemable_sessions_core")
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_defaults_to_original_course_and_filters_sessions(
		self,
		_require_admin,
		family_context,
		redeemable_sessions,
		label_map,
		_preview,
	):
		family_context.return_value = (SimpleNamespace(name="PAR-001"), [{"name": "STU-001"}], FakeVoucher())
		redeemable_sessions.return_value = {
			"students": [{"name": "STU-001"}],
			"selected_student": "STU-001",
			"available_sessions": [
				session("CS-001", "Anime"),
				session("CS-002", "Design"),
			],
		}
		label_map.return_value = {
			"Anime": {"course_name": "Anime"},
			"Design": {"course_name": "Design"},
		}

		result = get_school_admin_redeemable_sessions_data(
			parent="PAR-001",
			voucher_id="MV-001",
			student=None,
		)

		self.assertEqual(result["voucher_course"], "Anime")
		self.assertEqual(result["selected_course"], "Anime")
		self.assertEqual([row["session_id"] for row in result["available_sessions"]], ["CS-001"])
		self.assertEqual([row["course"] for row in result["eligible_courses"]], ["Anime", "Design"])

	@patch("qas_custom.services.school_admin.preview_makeup_target_pricing", return_value={"classification": "ordinary_cross_course"})
	@patch("qas_custom.services.school_admin._course_label_map")
	@patch("qas_custom.services.school_admin.get_parent_redeemable_sessions_core")
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_explicit_course_returns_only_that_courses_sessions(
		self,
		_require_admin,
		family_context,
		redeemable_sessions,
		label_map,
		_preview,
	):
		family_context.return_value = (SimpleNamespace(name="PAR-001"), [{"name": "STU-001"}], FakeVoucher())
		redeemable_sessions.return_value = {
			"students": [{"name": "STU-001"}],
			"selected_student": "STU-001",
			"available_sessions": [
				session("CS-001", "Anime"),
				session("CS-002", "Design"),
			],
		}
		label_map.return_value = {
			"Anime": {"course_name": "Anime"},
			"Design": {"course_name": "Design"},
		}

		result = get_school_admin_redeemable_sessions_data(
			parent="PAR-001",
			voucher_id="MV-001",
			course="Design",
		)

		self.assertEqual(result["selected_course"], "Design")
		self.assertEqual([row["session_id"] for row in result["available_sessions"]], ["CS-002"])

	@patch(
		"qas_custom.services.school_admin.frappe.throw",
		side_effect=RuntimeError("The selected course is no longer available"),
	)
	@patch("qas_custom.services.school_admin.preview_makeup_target_pricing", return_value={"classification": "same_course"})
	@patch("qas_custom.services.school_admin._course_label_map")
	@patch("qas_custom.services.school_admin.get_parent_redeemable_sessions_core")
	@patch("qas_custom.services.school_admin._get_school_admin_voucher_family_context")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_rejects_non_original_course_without_eligible_sessions(
		self,
		_require_admin,
		family_context,
		redeemable_sessions,
		label_map,
		_preview,
		_throw,
	):
		family_context.return_value = (SimpleNamespace(name="PAR-001"), [{"name": "STU-001"}], FakeVoucher())
		redeemable_sessions.return_value = {
			"students": [{"name": "STU-001"}],
			"selected_student": "STU-001",
			"available_sessions": [session("CS-001", "Anime")],
		}
		label_map.return_value = {"Anime": {"course_name": "Anime"}}

		with self.assertRaisesRegex(RuntimeError, "no longer available"):
			get_school_admin_redeemable_sessions_data(
				parent="PAR-001",
				voucher_id="MV-001",
				course="Design",
			)

	@patch("qas_custom.api.school_admin.get_school_admin_redeemable_sessions_data")
	def test_api_forwards_course_filter(self, service):
		service.return_value = {"available_sessions": []}

		result = school_admin_get_redeemable_sessions.__wrapped__(
			parent="PAR-001",
			voucher_id="MV-001",
			student="STU-001",
			course="Design",
		)

		self.assertEqual(result, {"available_sessions": []})
		service.assert_called_once_with(
			parent="PAR-001",
			voucher_id="MV-001",
			student="STU-001",
			course="Design",
		)
