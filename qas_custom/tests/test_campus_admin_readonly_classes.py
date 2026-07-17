from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.services.campus_admin import (
	_get_campus_admin_course_session_access,
	get_campus_admin_course_session_data,
	get_campus_admin_course_sessions_data,
)


class TestCampusAdminReadonlyClasses(TestCase):
	@patch("qas_custom.services.campus_admin._get_course_session_rows")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_session_list_queries_each_assigned_campus_and_merges_in_date_order(self, _labels, require_profile, get_rows):
		require_profile.return_value = {"campuses": ["Campus B", "Campus A"]}
		get_rows.side_effect = [
			[{"name": "SESSION-B", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "15:30:00", "campus": "Campus B"}}],
			[{"name": "SESSION-A", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "09:00:00", "campus": "Campus A"}}],
		]

		result = get_campus_admin_course_sessions_data(from_date="2026-07-17", to_date="2026-07-31")

		self.assertEqual([item["name"] for item in result["items"]], ["SESSION-A", "SESSION-B"])
		self.assertEqual(
			get_rows.call_args_list,
			[
				call(term=None, course=None, campus="Campus B", from_date="2026-07-17", to_date="2026-07-31", include_inactive_terms=0, include_inactive_timeslots=0, limit=160),
				call(term=None, course=None, campus="Campus A", from_date="2026-07-17", to_date="2026-07-31", include_inactive_terms=0, include_inactive_timeslots=0, limit=160),
			],
		)

	@patch("qas_custom.services.campus_admin._get_course_session_rows")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin.frappe.throw", side_effect=frappe.PermissionError)
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_session_list_rejects_unassigned_requested_campus(self, _labels, _throw, require_profile, get_rows):
		require_profile.return_value = {"campuses": ["Campus A"]}

		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_course_sessions_data(campus="Campus B")

		get_rows.assert_not_called()

	@patch("qas_custom.services.campus_admin.frappe.get_doc")
	@patch("qas_custom.services.campus_admin.frappe.throw", side_effect=frappe.PermissionError)
	def test_session_access_rejects_cross_campus_detail(self, _throw, get_doc):
		session = Mock()
		session.get.return_value = "TIMESLOT-1"
		timeslot = Mock()
		timeslot.get.return_value = "Campus B"
		get_doc.side_effect = [session, timeslot]

		with self.assertRaises(frappe.PermissionError):
			_get_campus_admin_course_session_access("SESSION-1", ["Campus A"])

	@patch("qas_custom.services.campus_admin._count_leave_attendance_rows", return_value=1)
	@patch("qas_custom.services.campus_admin._roster_course_session_attendance_rows")
	@patch("qas_custom.services.campus_admin._visible_course_session_attendance_rows")
	@patch("qas_custom.services.campus_admin._get_school_admin_attendance_rows")
	@patch("qas_custom.services.campus_admin._get_timeslot_summary")
	@patch("qas_custom.services.campus_admin._document_payload")
	@patch("qas_custom.services.campus_admin._get_campus_admin_course_session_access")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_allowed_session_detail_returns_read_only_roster_payload(
		self,
		_labels,
		require_profile,
		get_access,
		document_payload,
		get_timeslot,
		get_attendance,
		visible_rows,
		roster_rows,
		_count_leave,
	):
		require_profile.return_value = {"campuses": ["Campus A"]}
		session = SimpleNamespace(name="SESSION-1")
		timeslot = SimpleNamespace(name="TIMESLOT-1")
		get_access.return_value = (session, timeslot)
		document_payload.return_value = {"name": "SESSION-1", "teacher_override": ""}
		get_timeslot.return_value = {"term": "TERM-1", "teacher": "TEACHER-1", "campus": "Campus A"}
		attendance = [
			{"name": "ATT-1", "source_doctype": "Inquiry", "parent_phone": "0400", "has_outstanding_invoice": True},
			{"name": "ATT-2", "status": "Leave"},
		]
		get_attendance.return_value = attendance
		visible_rows.return_value = [attendance[0]]
		roster_rows.return_value = attendance

		payload = get_campus_admin_course_session_data("SESSION-1")

		self.assertEqual(payload["teacher"], "TEACHER-1")
		self.assertEqual(payload["student_count"], 1)
		self.assertEqual(payload["trial_count"], 1)
		self.assertEqual(payload["leave_count"], 1)
		self.assertEqual(payload["attendance"], attendance)
		self.assertNotIn("class_content", payload)
