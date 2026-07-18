from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from frappe.utils import getdate

from qas_custom.services.campus_admin import (
	_campus_admin_inquiry_date_filter,
	_campus_admin_inquiry_search_names,
	get_campus_admin_inquiries_data,
	get_campus_admin_inquiry_filter_options_data,
)


class TestCampusAdminInquiryAdvancedSearch(TestCase):
	def test_date_filter_intersects_upcoming_queue_and_custom_range(self):
		self.assertEqual(
			_campus_admin_inquiry_date_filter(
				[">=", getdate("2026-07-18")],
				from_date="2026-07-20",
				to_date="2026-07-26",
			),
			["between", [getdate("2026-07-20"), getdate("2026-07-26")]],
		)

	def test_date_filter_returns_no_match_for_range_outside_queue(self):
		self.assertFalse(
			_campus_admin_inquiry_date_filter(
				["<", getdate("2026-07-18")],
				from_date="2026-07-18",
				to_date="2026-07-20",
			)
		)

	def test_invalid_custom_date_range_is_rejected(self):
		fake_frappe = SimpleNamespace(
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message)))
		)
		with patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "From date cannot be later"):
				_campus_admin_inquiry_date_filter(from_date="2026-07-20", to_date="2026-07-18")

	@patch("qas_custom.services.campus_admin._get_latest_note_map", return_value={})
	@patch("qas_custom.services.campus_admin._campus_admin_inquiry_search_names", return_value=None)
	@patch("qas_custom.services.campus_admin.frappe.get_all", return_value=[])
	@patch("qas_custom.services.campus_admin.today", return_value="2026-07-18")
	@patch("qas_custom.services.campus_admin._filter_requested_campus", return_value=["Indooroopilly"])
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly", "Upper Mount Gravatt"]},
	)
	def test_list_combines_queue_date_course_campus_and_limit(
		self,
		_mock_profile,
		mock_filter_campus,
		_mock_today,
		mock_get_all,
		mock_search_names,
		_mock_latest_notes,
	):
		result = get_campus_admin_inquiries_data(
			queue="upcoming",
			status="Booked",
			inquiry_type="Trial Lesson",
			from_date="2026-07-20",
			to_date="2026-07-26",
			campus="Indooroopilly",
			course="Creative Art",
			query="Jayden",
			limit=500,
		)

		self.assertEqual(result, {"items": [], "has_more": False, "limit": 200})
		mock_filter_campus.assert_called_once_with(
			["Indooroopilly", "Upper Mount Gravatt"],
			"Indooroopilly",
		)
		search_filters = mock_search_names.call_args.args[0]
		self.assertEqual(search_filters["campus"], ["in", ["Indooroopilly"]])
		self.assertEqual(search_filters["status"], "Booked")
		self.assertEqual(search_filters["inquiry_type"], "Trial Lesson")
		self.assertEqual(search_filters["preferred_course"], "Creative Art")
		self.assertEqual(
			search_filters["current_appointment_date"],
			["between", [getdate("2026-07-20"), getdate("2026-07-26")]],
		)
		self.assertEqual(mock_search_names.call_args.args[2], "Jayden")
		self.assertEqual(mock_get_all.call_args.kwargs["limit_page_length"], 201)
		self.assertIn("current_appointment_time asc", mock_get_all.call_args.kwargs["order_by"])

	@patch("qas_custom.services.campus_admin._build_inquiry_list_item")
	@patch("qas_custom.services.campus_admin._get_latest_note_map", return_value={"INQ-001": "Latest"})
	@patch("qas_custom.services.campus_admin._campus_admin_inquiry_search_names", return_value=["INQ-001"])
	@patch("qas_custom.services.campus_admin.frappe.get_all")
	@patch("qas_custom.services.campus_admin.today", return_value="2026-07-18")
	@patch("qas_custom.services.campus_admin._filter_requested_campus", return_value=["Indooroopilly"])
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly"]},
	)
	def test_list_reports_has_more_and_batches_latest_notes(
		self,
		_mock_profile,
		_mock_filter_campus,
		_mock_today,
		mock_get_all,
		_mock_search_names,
		mock_latest_notes,
		mock_build_item,
	):
		rows = [SimpleNamespace(name=f"INQ-{index:03d}") for index in range(1, 202)]
		mock_get_all.return_value = rows
		mock_build_item.side_effect = lambda row, latest_note=None: {"id": row.name, "latest_note": latest_note}

		result = get_campus_admin_inquiries_data(query="Taylor")

		self.assertTrue(result["has_more"])
		self.assertEqual(result["limit"], 200)
		self.assertEqual(len(result["items"]), 200)
		mock_latest_notes.assert_called_once_with([row.name for row in rows[:200]])
		mock_build_item.assert_any_call(rows[0], latest_note="Latest")

	@patch("qas_custom.services.campus_admin._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.campus_admin.frappe.get_all")
	def test_open_text_search_uses_only_approved_fields_and_link_names(self, mock_get_all, _mock_safe_fields):
		def get_all_side_effect(doctype, **kwargs):
			if doctype == "Student":
				return ["STU-001"]
			if doctype == "Parent":
				return ["PAR-001"]
			filters = kwargs.get("filters", {})
			if any(condition[1] == "contact_email" for condition in (kwargs.get("or_filters") or [])):
				return ["INQ-EMAIL"]
			if filters.get("student") == ["in", ["STU-001"]]:
				return ["INQ-STUDENT"]
			if filters.get("parent") == ["in", ["PAR-001"]]:
				return ["INQ-PARENT"]
			return []

		mock_get_all.side_effect = get_all_side_effect
		names = _campus_admin_inquiry_search_names(
			{"campus": ["in", ["Indooroopilly"]]},
			None,
			"Taylor",
			order_by="current_appointment_date asc",
			limit=201,
		)

		self.assertEqual(set(names), {"INQ-STUDENT", "INQ-PARENT", "INQ-EMAIL"})
		direct_search_call = next(
			call for call in mock_get_all.call_args_list
			if call.args and call.args[0] == "Inquiry" and call.kwargs.get("or_filters")
		)
		searched_fields = {condition[1] for condition in direct_search_call.kwargs["or_filters"]}
		self.assertIn("contact_email", searched_fields)
		self.assertIn("submitted_student_name", searched_fields)
		self.assertNotIn("referral_detail", searched_fields)
		self.assertNotIn("preferred_course", searched_fields)

	@patch("qas_custom.services.campus_admin.frappe.get_all")
	@patch("qas_custom.services.campus_admin._filter_requested_campus", return_value=["Indooroopilly"])
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Indooroopilly", "Upper Mount Gravatt"]},
	)
	def test_filter_options_use_authorized_campus_and_historical_inquiry_courses(
		self,
		_mock_profile,
		mock_filter_campus,
		mock_get_all,
	):
		mock_get_all.return_value = [
			SimpleNamespace(preferred_course="Creative Art"),
			SimpleNamespace(preferred_course="Little Artist"),
		]

		result = get_campus_admin_inquiry_filter_options_data(campus="Indooroopilly")

		self.assertEqual(result["campuses"], ["Indooroopilly", "Upper Mount Gravatt"])
		self.assertEqual(result["courses"], ["Creative Art", "Little Artist"])
		mock_filter_campus.assert_called_once_with(
			["Indooroopilly", "Upper Mount Gravatt"],
			"Indooroopilly",
		)
		self.assertEqual(mock_get_all.call_args.kwargs["filters"]["campus"], ["in", ["Indooroopilly"]])
