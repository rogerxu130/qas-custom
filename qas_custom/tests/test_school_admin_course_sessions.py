from unittest import TestCase
from unittest.mock import patch

from frappe import _dict
from frappe.utils import getdate

from qas_custom.api.school_admin import school_admin_get_course_sessions
from qas_custom.services.school_admin import (
	_course_session_sort_key,
	_get_course_session_makeup_counts,
	_get_course_session_rows,
	get_school_admin_course_sessions_data,
)


class TestSchoolAdminCourseSessions(TestCase):
	def test_session_sort_key_orders_time_then_campus_course_and_name(self):
		rows = [
			{"name": "SESSION-LATE", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "15:30:00", "campus": "Campus A", "course": "Art"}},
			{"name": "SESSION-B", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "09:00:00", "campus": "Campus B", "course": "Art"}},
			{"name": "SESSION-A", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "09:00:00", "campus": "Campus A", "course": "Art"}},
		]

		result = sorted(rows, key=_course_session_sort_key)

		self.assertEqual([row["name"] for row in result], ["SESSION-A", "SESSION-B", "SESSION-LATE"])

	def test_session_sort_key_places_missing_or_invalid_time_last(self):
		rows = [
			{"name": "SESSION-MISSING", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": None}},
			{"name": "SESSION-INVALID", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "not-a-time"}},
			{"name": "SESSION-VALID", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "16:00:00"}},
		]

		result = sorted(rows, key=_course_session_sort_key)

		self.assertEqual(result[0]["name"], "SESSION-VALID")
		self.assertEqual({row["name"] for row in result[1:]}, {"SESSION-INVALID", "SESSION-MISSING"})

	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_makeup_counts_only_active_makeup_attendance(self, get_all, _doctype_available, _has_field):
		get_all.return_value = [
			_dict(course_session="SESSION-1", source_doctype="Makeup Voucher", makeup_voucher=None),
			_dict(course_session="SESSION-1", source_doctype=None, makeup_voucher="MV-1"),
			_dict(course_session="SESSION-2", source_doctype="Inquiry", makeup_voucher=None),
		]

		result = _get_course_session_makeup_counts(["SESSION-1", "SESSION-2"])

		self.assertEqual(result, {"SESSION-1": 2})
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"course_session": ["in", ["SESSION-1", "SESSION-2"]],
				"status": ["not in", ["Cancelled", "Leave"]],
			},
		)

	@patch("qas_custom.api.school_admin.get_school_admin_course_sessions_data")
	def test_api_accepts_and_passes_course_session_status_and_teacher(self, get_sessions):
		get_sessions.return_value = {"items": []}

		result = school_admin_get_course_sessions.__wrapped__(
			course="COURSE-1",
			campus="CAMPUS-1",
			teacher="TEACHER-1",
			status="Scheduled",
			from_date="2026-07-17",
		)

		self.assertEqual(result, {"items": []})
		get_sessions.assert_called_once_with(
			weekly_timeslot=None,
			term=None,
			course="COURSE-1",
			campus="CAMPUS-1",
			teacher="TEACHER-1",
			status="Scheduled",
			from_date="2026-07-17",
			to_date=None,
			include_inactive_terms=0,
			include_inactive_timeslots=0,
			limit=160,
		)

	@patch("qas_custom.services.school_admin._get_course_session_rows")
	@patch("qas_custom.services.school_admin._require_school_admin")
	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	def test_service_passes_scheduled_status_and_teacher_to_session_query(self, _doctype, _require, get_rows):
		get_rows.return_value = []

		result = get_school_admin_course_sessions_data(
			course="COURSE-1",
			campus="CAMPUS-1",
			teacher="TEACHER-1",
			status="Scheduled",
			from_date="2026-07-17",
			limit=1000,
		)

		self.assertEqual(result, {"items": []})
		get_rows.assert_called_once_with(
			weekly_timeslot=None,
			term=None,
			course="COURSE-1",
			campus="CAMPUS-1",
			teacher="TEACHER-1",
			status="Scheduled",
			from_date="2026-07-17",
			to_date=None,
			include_inactive_terms=0,
			include_inactive_timeslots=0,
			limit=1000,
		)

	@patch("qas_custom.services.school_admin._filter_timeslots_for_session_query", return_value=["WT-1"])
	@patch("qas_custom.services.school_admin._safe_fields", return_value=["name", "weekly_timeslot", "session_date", "status"])
	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe.get_all", return_value=[])
	def test_session_query_filters_scheduled_status_with_active_timeslots(
		self,
		get_all,
		_doctype,
		_safe_fields,
		filter_timeslots,
	):
		result = _get_course_session_rows(
			course="COURSE-1",
			campus="CAMPUS-1",
			status="Scheduled",
			from_date="2026-07-17",
		)

		self.assertEqual(result, [])
		filter_timeslots.assert_called_once_with(
			term=None,
			course="COURSE-1",
			campus="CAMPUS-1",
			include_inactive_terms=0,
			include_inactive_timeslots=0,
		)
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"status": "Scheduled",
				"session_date": [">=", getdate("2026-07-17")],
				"weekly_timeslot": ["in", ["WT-1"]],
			},
		)

	@patch("qas_custom.services.school_admin._get_course_session_leave_counts", return_value={})
	@patch("qas_custom.services.school_admin._get_course_session_makeup_counts", return_value={})
	@patch("qas_custom.services.school_admin._get_course_session_trial_counts", return_value={})
	@patch("qas_custom.services.school_admin._get_course_session_student_counts", return_value={})
	@patch("qas_custom.services.school_admin._get_timeslot_map")
	@patch("qas_custom.services.school_admin._safe_fields", return_value=["name", "weekly_timeslot", "session_date", "status", "teacher_override"])
	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_session_query_filters_by_actual_teacher_including_override(
		self,
		get_all,
		_doctype,
		_safe_fields,
		get_timeslot_map,
		_student_counts,
		_trial_counts,
		_makeup_counts,
		_leave_counts,
	):
		get_all.return_value = [
			_dict(name="SESSION-WEEKLY", weekly_timeslot="WT-1", session_date="2026-08-01", status="Scheduled", teacher_override=None),
			_dict(name="SESSION-OVERRIDE", weekly_timeslot="WT-2", session_date="2026-08-01", status="Scheduled", teacher_override="TEACHER-1"),
			_dict(name="SESSION-OTHER", weekly_timeslot="WT-3", session_date="2026-08-01", status="Scheduled", teacher_override=None),
		]
		get_timeslot_map.return_value = {
			"WT-1": {"name": "WT-1", "teacher": "TEACHER-1", "course": "COURSE-1"},
			"WT-2": {"name": "WT-2", "teacher": "TEACHER-2", "course": "COURSE-1"},
			"WT-3": {"name": "WT-3", "teacher": "TEACHER-3", "course": "COURSE-1"},
		}

		result = _get_course_session_rows(teacher="TEACHER-1", limit=160)

		self.assertEqual([item["name"] for item in result], ["SESSION-OVERRIDE", "SESSION-WEEKLY"])
		self.assertEqual(get_all.call_args.kwargs["limit"], 3000)
