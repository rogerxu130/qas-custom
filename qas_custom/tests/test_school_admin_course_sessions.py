from unittest import TestCase
from unittest.mock import patch

from frappe.utils import getdate

from qas_custom.api.school_admin import school_admin_get_course_sessions
from qas_custom.services.school_admin import (
	_get_course_session_rows,
	get_school_admin_course_sessions_data,
)


class TestSchoolAdminCourseSessions(TestCase):
	@patch("qas_custom.api.school_admin.get_school_admin_course_sessions_data")
	def test_api_accepts_and_passes_course_session_status(self, get_sessions):
		get_sessions.return_value = {"items": []}

		result = school_admin_get_course_sessions.__wrapped__(
			course="COURSE-1",
			campus="CAMPUS-1",
			status="Scheduled",
			from_date="2026-07-17",
		)

		self.assertEqual(result, {"items": []})
		get_sessions.assert_called_once_with(
			weekly_timeslot=None,
			term=None,
			course="COURSE-1",
			campus="CAMPUS-1",
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
	def test_service_passes_scheduled_status_to_session_query(self, _doctype, _require, get_rows):
		get_rows.return_value = []

		result = get_school_admin_course_sessions_data(
			course="COURSE-1",
			campus="CAMPUS-1",
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
