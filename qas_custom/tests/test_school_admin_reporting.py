from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin_reporting import (
	FAMILY_REPORT_TYPE,
	UNMARKED_REPORT_TYPE,
	_attendance_classification,
	_attendance_counts,
	_build_reporting_rows,
	_invoice_summary,
	_session_end_datetime,
	_session_in_completed_range,
	_session_in_unmarked_window,
	_require_school_admin,
	get_school_admin_reporting_rows_data,
	start_school_admin_reporting_generation_data,
)


class TestSchoolAdminReportingClassifications(TestCase):
	def test_attendance_classification_priority_is_mutually_exclusive(self):
		self.assertEqual(_attendance_classification({"present_late_count": 1, "absent_count": 5}), "Attended")
		self.assertEqual(_attendance_classification({"present_late_count": 0, "absent_count": 1, "leave_count": 5}), "Absent")
		self.assertEqual(_attendance_classification({"present_late_count": 0, "absent_count": 0, "leave_count": 1}), "Leave")
		self.assertEqual(_attendance_classification({"cancelled_count": 1}), "Cancelled only")
		self.assertEqual(_attendance_classification({}), "No attendance records")

	def test_attendance_counts_ignore_unmarked_rows(self):
		counts = _attendance_counts(
			[
				{"status": "Present"},
				{"status": "Late"},
				{"status": "Absent"},
				{"status": "Leave"},
				{"status": "Cancelled"},
				{"status": "To be started"},
			]
		)
		self.assertEqual(
			counts,
			{
				"present_late_count": 2,
				"absent_count": 1,
				"leave_count": 1,
				"cancelled_count": 1,
				"attendance_total": 5,
			},
		)

	@patch("qas_custom.services.school_admin_reporting.get_invoice_payable_amount")
	def test_invoice_priority_outstanding_then_draft_then_paid(self, mock_payable):
		mock_payable.side_effect = lambda row: row.get("test_payable")
		outstanding = _invoice_summary(
			[
				frappe._dict(name="SINV-1", docstatus=1, status="Unpaid", test_payable=100),
				frappe._dict(name="SINV-2", docstatus=0, status="Draft", test_payable=50),
			]
		)
		self.assertEqual(outstanding["classification"], "Outstanding")
		self.assertEqual(outstanding["outstanding_amount"], 100)

		draft = _invoice_summary(
			[
				frappe._dict(name="SINV-3", docstatus=1, status="Paid", test_payable=0),
				frappe._dict(name="SINV-4", docstatus=0, status="Draft", test_payable=50),
			]
		)
		self.assertEqual(draft["classification"], "Draft Invoice")

		paid = _invoice_summary([frappe._dict(name="SINV-5", docstatus=1, status="Paid", test_payable=0)])
		self.assertEqual(paid["classification"], "Not Outstanding")


class TestSchoolAdminReportingTimeBoundaries(TestCase):
	def setUp(self):
		self.session = {"name": "CS-1", "session_date": "2026-07-22", "status": "Scheduled", "weekly_timeslot": "WT-1"}
		self.timeslot = {"start_time": "16:00:00", "end_time": "17:30:00"}

	def test_session_end_uses_end_time_and_start_time_fallback(self):
		self.assertEqual(_session_end_datetime(self.session, self.timeslot), datetime(2026, 7, 22, 17, 30))
		self.assertEqual(
			_session_end_datetime(self.session, {"start_time": timedelta(hours=16)}),
			datetime(2026, 7, 22, 16, 0),
		)

	def test_current_day_session_is_excluded_until_class_finishes(self):
		timeslots = {"WT-1": self.timeslot}
		self.assertFalse(
			_session_in_completed_range(
				self.session,
				timeslots,
				term_start=datetime(2026, 7, 1).date(),
				term_end=datetime(2026, 7, 22).date(),
				generated_at=datetime(2026, 7, 22, 17, 0),
			)
		)
		self.assertTrue(
			_session_in_completed_range(
				self.session,
				timeslots,
				term_start=datetime(2026, 7, 1).date(),
				term_end=datetime(2026, 7, 22).date(),
				generated_at=datetime(2026, 7, 22, 18, 0),
			)
		)

	def test_unmarked_window_is_inclusive_seven_calendar_days(self):
		timeslots = {"WT-1": self.timeslot}
		generated = datetime(2026, 7, 22, 18, 0)
		self.session["session_date"] = "2026-07-16"
		self.assertTrue(
			_session_in_unmarked_window(
				self.session,
				timeslots,
				datetime(2026, 7, 1).date(),
				datetime(2026, 7, 22).date(),
				datetime(2026, 7, 16).date(),
				generated,
			)
		)
		self.session["session_date"] = "2026-07-15"
		self.assertFalse(
			_session_in_unmarked_window(
				self.session,
				timeslots,
				datetime(2026, 7, 1).date(),
				datetime(2026, 7, 22).date(),
				datetime(2026, 7, 16).date(),
				generated,
			)
		)

	def test_cancelled_session_is_excluded(self):
		self.session["status"] = "Cancelled"
		self.assertFalse(
			_session_in_completed_range(
				self.session,
				{"WT-1": self.timeslot},
				datetime(2026, 7, 1).date(),
				datetime(2026, 7, 22).date(),
				datetime(2026, 7, 22, 18, 0),
			)
		)


class TestSchoolAdminReportingBuild(TestCase):
	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting._term_invoice_map")
	@patch("qas_custom.services.school_admin_reporting._session_context")
	@patch("qas_custom.services.school_admin_reporting._attendance_rows")
	@patch("qas_custom.services.school_admin_reporting._parent_map")
	@patch("qas_custom.services.school_admin_reporting._student_map")
	@patch("qas_custom.services.school_admin_reporting._student_parent_field", return_value="guardian")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_doc")
	def test_builds_one_family_row_and_one_unmarked_row(
		self,
		mock_get_doc,
		mock_get_all,
		_mock_parent_field,
		mock_students,
		mock_parents,
		mock_attendance,
		mock_sessions,
		mock_invoices,
		_mock_safe_fields,
	):
		mock_get_doc.return_value = SimpleNamespace(start_date="2026-07-01", end_date="2026-09-30")
		mock_get_all.return_value = [
			frappe._dict(name="ENR-1", student="STU-1", parent="PAR-1", weekly_timeslot="WT-1", status="Active")
		]
		mock_students.return_value = {"STU-1": {"name": "STU-1", "student_name": "Sam Student", "guardian": "PAR-1"}}
		mock_parents.return_value = {
			"PAR-1": {"name": "PAR-1", "parent_name": "Pat Parent", "email": "pat@example.com", "phone": "0400", "customer": "CUS-1"}
		}
		mock_attendance.return_value = [
			frappe._dict(name="ATT-1", source_document="ENR-1", student="STU-1", status="Present", course_session="CS-1"),
			frappe._dict(name="ATT-2", source_document="ENR-1", student="STU-1", status="To be started", course_session="CS-2"),
		]
		mock_sessions.return_value = (
			{
				"CS-1": {"name": "CS-1", "weekly_timeslot": "WT-1", "session_date": "2026-07-14", "status": "Completed"},
				"CS-2": {"name": "CS-2", "weekly_timeslot": "WT-1", "session_date": "2026-07-20", "status": "Completed"},
			},
			{
				"WT-1": {
					"name": "WT-1",
					"course": "Creative Art",
					"campus": "Indooroopilly",
					"teacher": "TEA-1",
					"start_time": time(16, 0),
					"end_time": time(17, 30),
				}
			},
		)
		mock_invoices.return_value = {
			"PAR-1": {"classification": "Outstanding", "outstanding_amount": 200, "invoices": [{"name": "SINV-1"}]}
		}

		result = _build_reporting_rows("Term 3 2026", datetime(2026, 7, 22, 18, 0))

		self.assertEqual(len(result["family_rows"]), 1)
		self.assertEqual(result["family_rows"][0]["attendance_classification"], "Attended")
		self.assertEqual(result["family_rows"][0]["outstanding_amount"], 200)
		self.assertIn("sam student", result["family_rows"][0]["search_text"])
		self.assertEqual(len(result["unmarked_rows"]), 1)
		self.assertEqual(result["unmarked_rows"][0]["attendance_entry"], "ATT-2")
		self.assertEqual(result["unmarked_rows"][0]["teacher"], "TEA-1")
		self.assertEqual(result["unmarked_rows"][0]["invoice_classification"], "Outstanding")

		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["term"], "Term 3 2026")
		self.assertEqual(filters["status"], ["in", ["Active", "Planned", "Completed"]])

	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting._term_invoice_map", return_value={})
	@patch("qas_custom.services.school_admin_reporting._session_context", return_value=({}, {}))
	@patch("qas_custom.services.school_admin_reporting._attendance_rows", return_value=[])
	@patch("qas_custom.services.school_admin_reporting._parent_map", return_value={"PAR-1": {"parent_name": "Pat"}})
	@patch("qas_custom.services.school_admin_reporting._student_map", return_value={"STU-1": {"student_name": "Sam"}})
	@patch("qas_custom.services.school_admin_reporting._student_parent_field", return_value="guardian")
	@patch(
		"qas_custom.services.school_admin_reporting.frappe.get_all",
		return_value=[frappe._dict(name="ENR-1", student="STU-1", parent="PAR-1")],
	)
	@patch(
		"qas_custom.services.school_admin_reporting.frappe.get_doc",
		return_value=SimpleNamespace(start_date="2026-07-01", end_date="2026-09-30"),
	)
	def test_family_without_completed_attendance_is_reported(self, *_mocks):
		result = _build_reporting_rows("Term 3 2026", datetime(2026, 7, 22, 18, 0))
		row = result["family_rows"][0]
		self.assertEqual(row["attendance_classification"], "No attendance records")
		self.assertEqual(row["invoice_classification"], "No Invoice")


class TestSchoolAdminReportingActions(TestCase):
	@patch("qas_custom.services.school_admin_reporting.frappe.get_roles", return_value=["Campus Admin"])
	def test_non_school_admin_is_denied(self, _mock_roles):
		with self.assertRaises(frappe.PermissionError):
			_require_school_admin()

	@patch("qas_custom.services.school_admin_reporting.frappe.get_roles", return_value=["School Admin"])
	def test_school_admin_is_allowed(self, _mock_roles):
		_require_school_admin()

	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._assert_reporting_doctypes")
	@patch("qas_custom.services.school_admin_reporting._running_snapshot")
	def test_start_reuses_running_generation(self, mock_running, _mock_assert, _mock_validate, _mock_require):
		mock_running.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Running")
		result = start_school_admin_reporting_generation_data("Term 3 2026")
		self.assertTrue(result["reused"])
		self.assertEqual(result["snapshot"]["name"], "QARS-1")

	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={"campuses": [], "teachers": []})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting.frappe.db.count", return_value=1)
	def test_family_row_filters_are_applied_to_latest_snapshot(
		self,
		_mock_count,
		mock_get_all,
		_mock_require,
		_mock_validate,
		mock_latest,
		_mock_options,
	):
		mock_latest.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Completed")
		mock_get_all.return_value = []
		result = get_school_admin_reporting_rows_data(
			term="Term 3 2026",
			report_type=FAMILY_REPORT_TYPE,
			attendance="Absent",
			invoice="Outstanding",
			query="pat",
		)
		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["snapshot"], "QARS-1")
		self.assertEqual(filters["attendance_classification"], "Absent")
		self.assertEqual(filters["invoice_classification"], "Outstanding")
		self.assertEqual(filters["search_text"], ["like", "%pat%"])
		self.assertEqual(result["total"], 1)

	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={"campuses": [], "teachers": []})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all", return_value=[])
	@patch("qas_custom.services.school_admin_reporting.frappe.db.count", return_value=0)
	def test_unmarked_filters_are_applied(
		self,
		_mock_count,
		mock_get_all,
		_mock_require,
		_mock_validate,
		mock_latest,
		_mock_options,
	):
		mock_latest.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Completed")
		get_school_admin_reporting_rows_data(
			term="Term 3 2026",
			report_type=UNMARKED_REPORT_TYPE,
			campus="Indooroopilly",
			teacher="TEA-1",
		)
		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["campus"], "Indooroopilly")
		self.assertEqual(filters["teacher"], "TEA-1")
