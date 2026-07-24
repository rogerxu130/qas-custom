from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.api.campus_admin import (
	campus_admin_get_linkable_enrollments,
	campus_admin_link_inquiry_enrollment,
)
from qas_custom.services.campus_admin import (
	_validate_linkable_enrollment_access,
	get_campus_admin_linkable_enrollments_data,
	link_campus_admin_inquiry_enrollment_data,
)


class TestCampusAdminExistingEnrollmentConversion(TestCase):
	def _inquiry(self, **values):
		return frappe._dict(
			{
				"name": "INQ-001",
				"inquiry_type": "Trial Lesson",
				"status": "Completed",
				"student": "STU-001",
				"parent": "PAR-001",
				"course_session": "CS-TRIAL",
				"converted_enrollment": "",
				**values,
			}
		)

	def _enrollment(self, **values):
		return frappe._dict(
			{
				"name": "ENR-001",
				"student": "STU-001",
				"parent": "PAR-001",
				"term": "TERM-3",
				"course": "COURSE-A",
				"weekly_timeslot": "WT-001",
				"status": "Active",
				"invoice": "SINV-001",
				"source_inquiry": "",
				**values,
			}
		)

	def _timeslot(self, **values):
		return frappe._dict(
			{
				"name": "WT-001",
				"term": "TERM-3",
				"course": "COURSE-A",
				"campus": "Campus A",
				"classroom": "Room 1",
				"teacher": "Teacher A",
				"day_of_week": "Monday",
				"start_time": "16:00:00",
				"end_time": "17:30:00",
				**values,
			}
		)

	@patch("qas_custom.services.campus_admin._get_inquiry_link_context")
	@patch("qas_custom.services.campus_admin._require_inquiry_access")
	@patch("qas_custom.services.campus_admin.frappe.get_all")
	@patch("qas_custom.services.campus_admin._", side_effect=lambda message: message)
	def test_candidate_query_is_limited_to_student_term_status_and_assigned_campuses(
		self,
		_translate,
		get_all,
		require_access,
		get_context,
	):
		inquiry = self._inquiry()
		require_access.return_value = {"campuses": ["Campus A", "Campus B"]}
		get_context.return_value = (inquiry, "TERM-3")
		get_all.side_effect = [
			[self._timeslot()],
			[self._enrollment()],
			[],
		]

		result = get_campus_admin_linkable_enrollments_data("INQ-001")

		self.assertEqual(result["term"], "TERM-3")
		self.assertEqual(len(result["items"]), 1)
		self.assertEqual(result["items"][0]["name"], "ENR-001")
		self.assertEqual(result["items"][0]["campus"], "Campus A")
		self.assertEqual(result["items"][0]["invoice"], "SINV-001")
		timeslot_filters = get_all.call_args_list[0].kwargs["filters"]
		self.assertEqual(timeslot_filters["campus"], ["in", ["Campus A", "Campus B"]])
		self.assertEqual(timeslot_filters["term"], "TERM-3")
		enrollment_filters = get_all.call_args_list[1].kwargs["filters"]
		self.assertEqual(enrollment_filters["student"], "STU-001")
		self.assertEqual(enrollment_filters["term"], "TERM-3")
		self.assertEqual(enrollment_filters["status"], ["in", ["Active", "Planned"]])
		self.assertEqual(enrollment_filters["weekly_timeslot"], ["in", ["WT-001"]])

	@patch("qas_custom.services.campus_admin._get_inquiry_link_context")
	@patch("qas_custom.services.campus_admin._require_inquiry_access")
	@patch("qas_custom.services.campus_admin.frappe.get_all")
	@patch("qas_custom.services.campus_admin._", side_effect=lambda message: message)
	def test_candidate_query_excludes_other_parent_claimed_and_converted_enrollments(
		self,
		_translate,
		get_all,
		require_access,
		get_context,
	):
		inquiry = self._inquiry()
		require_access.return_value = {"campuses": ["Campus A"]}
		get_context.return_value = (inquiry, "TERM-3")
		get_all.side_effect = [
			[self._timeslot()],
			[
				self._enrollment(name="ENR-PARENT", parent="PAR-OTHER"),
				self._enrollment(name="ENR-CLAIMED", source_inquiry="INQ-OTHER"),
				self._enrollment(name="ENR-CONVERTED"),
			],
			[frappe._dict(name="INQ-OTHER", converted_enrollment="ENR-CONVERTED")],
		]

		result = get_campus_admin_linkable_enrollments_data("INQ-001")

		self.assertEqual(result["items"], [])

	@patch("qas_custom.services.campus_admin.link_existing_enrollment_core")
	@patch("qas_custom.services.campus_admin._validate_linkable_enrollment_access")
	@patch("qas_custom.services.campus_admin._require_inquiry_access")
	@patch("qas_custom.services.campus_admin.reject_support_view_write")
	@patch("qas_custom.services.campus_admin._", side_effect=lambda message: message)
	def test_link_wrapper_enforces_access_and_uses_shared_core(
		self,
		_translate,
		reject_support_view,
		require_access,
		validate_access,
		link_core,
	):
		profile = {"campuses": ["Campus A"]}
		require_access.return_value = profile
		link_core.return_value = {"inquiry": {"id": "INQ-001", "status": "Converted"}}
		with patch(
			"qas_custom.services.campus_admin.frappe.session",
			SimpleNamespace(user="campus@example.com"),
		):
			result = link_campus_admin_inquiry_enrollment_data("INQ-001", "ENR-001")

		reject_support_view.assert_called_once_with()
		require_access.assert_called_once_with("INQ-001")
		validate_access.assert_called_once_with("INQ-001", "ENR-001", profile)
		link_core.assert_called_once_with(
			"INQ-001",
			"ENR-001",
			actor="campus@example.com",
			operator_label="Campus Admin",
		)
		self.assertEqual(result["inquiry"]["status"], "Converted")

	def test_validation_rejects_enrollment_outside_assigned_campus(self):
		inquiry = self._inquiry()
		enrollment = self._enrollment()
		fake_frappe = SimpleNamespace(
			get_doc=Mock(side_effect=[inquiry, enrollment]),
			db=SimpleNamespace(
				get_value=Mock(return_value=frappe._dict(campus="Campus B", term="TERM-3"))
			),
			throw=lambda message, *args, **kwargs: (_ for _ in ()).throw(
				frappe.ValidationError(str(message))
			),
			PermissionError=frappe.PermissionError,
		)

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe), patch(
			"qas_custom.services.campus_admin._get_inquiry_link_context",
			return_value=(inquiry, "TERM-3"),
		), patch("qas_custom.services.campus_admin._", side_effect=lambda message: message):
			with self.assertRaisesRegex(frappe.ValidationError, "selected Enrollment campus"):
				_validate_linkable_enrollment_access(
					"INQ-001",
					"ENR-001",
					{"campuses": ["Campus A"]},
				)

	def test_validation_rejects_enrollment_from_another_term(self):
		inquiry = self._inquiry()
		enrollment = self._enrollment(term="TERM-2")
		fake_frappe = SimpleNamespace(
			get_doc=Mock(side_effect=[inquiry, enrollment]),
			db=SimpleNamespace(
				get_value=Mock(return_value=frappe._dict(campus="Campus A", term="TERM-3"))
			),
			throw=lambda message, *args, **kwargs: (_ for _ in ()).throw(
				frappe.ValidationError(str(message))
			),
			PermissionError=frappe.PermissionError,
		)

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe), patch(
			"qas_custom.services.campus_admin._get_inquiry_link_context",
			return_value=(inquiry, "TERM-3"),
		), patch("qas_custom.services.campus_admin._", side_effect=lambda message: message):
			with self.assertRaisesRegex(frappe.ValidationError, "same Term"):
				_validate_linkable_enrollment_access(
					"INQ-001",
					"ENR-001",
					{"campuses": ["Campus A"]},
				)

	def test_validation_keeps_repeat_link_idempotent(self):
		inquiry = self._inquiry(status="Converted", converted_enrollment="ENR-001")
		enrollment = self._enrollment(status="Cancelled", term="TERM-2")
		fake_frappe = SimpleNamespace(
			get_doc=Mock(side_effect=[inquiry, enrollment]),
			db=SimpleNamespace(
				get_value=Mock(return_value=frappe._dict(campus="Campus A", term="TERM-2"))
			),
			throw=lambda message, *args, **kwargs: (_ for _ in ()).throw(
				frappe.ValidationError(str(message))
			),
			PermissionError=frappe.PermissionError,
		)

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe), patch(
			"qas_custom.services.campus_admin._get_inquiry_link_context",
			return_value=(inquiry, "TERM-3"),
		), patch("qas_custom.services.campus_admin._", side_effect=lambda message: message):
			self.assertIsNone(
				_validate_linkable_enrollment_access(
					"INQ-001",
					"ENR-001",
					{"campuses": ["Campus A"]},
				)
			)

	@patch("qas_custom.api.campus_admin.get_campus_admin_linkable_enrollments_data")
	def test_candidate_api_passes_inquiry(self, get_candidates):
		get_candidates.return_value = {"items": [], "term": "TERM-3"}

		result = campus_admin_get_linkable_enrollments.__wrapped__(inquiry="INQ-001")

		self.assertEqual(result["term"], "TERM-3")
		get_candidates.assert_called_once_with(inquiry="INQ-001")

	@patch("qas_custom.api.campus_admin.link_campus_admin_inquiry_enrollment_data")
	def test_link_api_passes_inquiry_and_enrollment(self, link):
		link.return_value = {"inquiry": {"id": "INQ-001", "status": "Converted"}}

		result = campus_admin_link_inquiry_enrollment.__wrapped__(
			inquiry="INQ-001",
			enrollment="ENR-001",
		)

		self.assertEqual(result["inquiry"]["status"], "Converted")
		link.assert_called_once_with(inquiry="INQ-001", enrollment="ENR-001")
