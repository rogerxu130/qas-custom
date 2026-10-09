from datetime import datetime, time, timedelta
import json
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
	_enrollment_class_schedules,
	_family_row_names_for_weekday,
	export_school_admin_reporting_families_data,
	_invoice_summary,
	_parent_map,
	_session_end_datetime,
	_session_in_completed_range,
	_session_in_unmarked_window,
	_require_school_admin,
	get_school_admin_voucher_report_data,
	get_school_admin_term_paid_invoice_summary_data,
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
		self.assertEqual(_attendance_classification({"to_be_started_count": 1}), "To be started")
		self.assertEqual(_attendance_classification({"present_late_count": 1, "to_be_started_count": 2}), "Attended")

	def test_attendance_counts_track_unmarked_separately_from_marked_total(self):
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
				"to_be_started_count": 1,
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
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	def test_parent_map_uses_linked_user_email(self, mock_get_all, _mock_safe_fields):
		mock_get_all.side_effect = [
			[frappe._dict(name="PAR-1", parent_name="Pat Parent", linked_user="pat@example.com", mobile_number="0400")],
			[frappe._dict(name="pat@example.com", email="pat@example.com")],
		]

		result = _parent_map(["PAR-1"])

		self.assertEqual(result["PAR-1"]["email"], "pat@example.com")
		self.assertEqual(result["PAR-1"]["phone"], "0400")
		self.assertEqual(mock_get_all.call_args_list[1].args[0], "User")
		self.assertEqual(mock_get_all.call_args_list[1].kwargs["filters"], {"name": ["in", ["pat@example.com"]]})

	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting._term_invoice_map")
	@patch("qas_custom.services.school_admin_reporting._enrollment_class_schedules", return_value={"STU-1": [{"day_of_week": "Tuesday", "course": "Creative Art"}]})
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
		mock_class_schedules,
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
			frappe._dict(name="ATT-3", source_document="ENR-1", student="STU-1", status="To be started", course_session="CS-FUTURE"),
		]
		mock_sessions.return_value = (
			{
				"CS-1": {"name": "CS-1", "weekly_timeslot": "WT-1", "session_date": "2026-07-14", "status": "Completed"},
				"CS-2": {"name": "CS-2", "weekly_timeslot": "WT-1", "session_date": "2026-07-20", "status": "Completed"},
				"CS-FUTURE": {"name": "CS-FUTURE", "weekly_timeslot": "WT-1", "session_date": "2026-07-23", "status": "Scheduled"},
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

		self.assertEqual(json.loads(result["family_rows"][0]["student_details_json"])[0]["class_schedule"], [{"day_of_week": "Tuesday", "course": "Creative Art"}])
		mock_class_schedules.assert_called_once_with(mock_get_all.return_value)
		self.assertEqual(len(result["family_rows"]), 1)
		self.assertEqual(result["family_rows"][0]["attendance_classification"], "Attended")
		self.assertEqual(result["family_rows"][0]["outstanding_amount"], 200)
		self.assertEqual(result["family_rows"][0]["to_be_started_count"], 1)
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
	@patch("qas_custom.services.school_admin_reporting._enrollment_class_schedules", return_value={})
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


class TestSchoolAdminReportingClassSchedules(TestCase):
	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	def test_keeps_day_and_course_paired_for_each_student_and_deduplicates(self, mock_get_all, _mock_fields):
		mock_get_all.return_value = [
			{"name": "WT-1", "course": "Drawing", "day_of_week": "Wednesday"},
			{"name": "WT-2", "course": "Painting", "day_of_week": "Saturday"},
		]
		enrollments = [
			{"student": "Amy", "weekly_timeslot": "WT-2"},
			{"student": "Amy", "weekly_timeslot": "WT-1"},
			{"student": "Amy", "weekly_timeslot": "WT-1"},
			{"student": "Ben", "weekly_timeslot": "WT-2"},
		]
		self.assertEqual(_enrollment_class_schedules(enrollments), {
			"Amy": [{"day_of_week": "Wednesday", "course": "Drawing"}, {"day_of_week": "Saturday", "course": "Painting"}],
			"Ben": [{"day_of_week": "Saturday", "course": "Painting"}],
		})
		self.assertEqual(mock_get_all.call_count, 1)

	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	def test_unassigned_course_is_retained_without_inventing_a_weekday(self, mock_get_all):
		self.assertEqual(_enrollment_class_schedules([{"student": "Amy", "course": "Drawing"}]), {"Amy": [{"day_of_week": "", "course": "Drawing"}]})
		mock_get_all.assert_not_called()
		self.assertEqual(_enrollment_class_schedules([]), {})


class TestSchoolAdminReportingActions(TestCase):
	def test_non_school_admin_is_denied(self):
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="campus@example.com"),
			get_roles=Mock(return_value=["Campus Admin"]),
			PermissionError=frappe.PermissionError,
			throw=Mock(side_effect=frappe.PermissionError),
		)
		with patch("qas_custom.services.school_admin_reporting.frappe", fake_frappe):
			with self.assertRaises(frappe.PermissionError):
				_require_school_admin()

	def test_school_admin_is_allowed(self):
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="school@example.com"),
			get_roles=Mock(return_value=["School Admin"]),
			throw=Mock(),
		)
		with patch("qas_custom.services.school_admin_reporting.frappe", fake_frappe):
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


class TestSchoolAdminVoucherReport(TestCase):
	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting.today", return_value="2026-07-27")
	@patch("qas_custom.services.school_admin_reporting._voucher_session_map", return_value={})
	@patch("qas_custom.services.school_admin_reporting._parent_map")
	@patch("qas_custom.services.school_admin_reporting._student_map")
	@patch("qas_custom.services.school_admin_reporting._student_parent_field", return_value="guardian")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_usable_excludes_a_valid_but_expired_voucher(
		self,
		_mock_require,
		_mock_doctype,
		mock_get_all,
		_mock_parent_field,
		mock_students,
		mock_parents,
		_mock_sessions,
		_mock_today,
		_mock_safe_fields,
	):
		mock_get_all.return_value = [
			frappe._dict(name="MV-VALID", student="STU-1", course="Art", status="Valid", expiry_date="2026-07-28"),
			frappe._dict(name="MV-OLD", student="STU-1", course="Art", status="Valid", expiry_date="2026-07-26"),
		]
		mock_students.return_value = {"STU-1": {"student_name": "Sam", "guardian": "PAR-1"}}
		mock_parents.return_value = {"PAR-1": {"parent_name": "Pat", "email": "pat@example.com", "phone": "0400"}}

		usable = get_school_admin_voucher_report_data(status="Usable")
		expired = get_school_admin_voucher_report_data(status="Expired")

		self.assertEqual([row["name"] for row in usable["items"]], ["MV-VALID"])
		self.assertEqual([row["name"] for row in expired["items"]], ["MV-OLD"])
		self.assertEqual(expired["items"][0]["status"], "Expired")

	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting._voucher_session_map", return_value={})
	@patch("qas_custom.services.school_admin_reporting._parent_map")
	@patch("qas_custom.services.school_admin_reporting._student_map")
	@patch("qas_custom.services.school_admin_reporting._student_parent_field", return_value="guardian")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_search_matches_parent_contact_and_paginates(
		self,
		_mock_require,
		_mock_doctype,
		mock_get_all,
		_mock_parent_field,
		mock_students,
		mock_parents,
		_mock_sessions,
		_mock_safe_fields,
	):
		mock_get_all.return_value = [
			frappe._dict(name="MV-001", student="STU-1", status="Used"),
			frappe._dict(name="MV-002", student="STU-1", status="Cancelled"),
		]
		mock_students.return_value = {"STU-1": {"student_name": "Sam", "guardian": "PAR-1"}}
		mock_parents.return_value = {"PAR-1": {"parent_name": "Pat", "email": "pat@example.com", "phone": "0400"}}

		result = get_school_admin_voucher_report_data(status="All", query="pat@example", page=2, page_length=1)

		self.assertEqual(result["total"], 2)
		self.assertEqual(result["items"][0]["name"], "MV-002")
		self.assertFalse(result["has_more"])


class TestSchoolAdminTermPaidInvoiceSummary(TestCase):
	@patch("qas_custom.services.school_admin_reporting._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin_reporting.get_invoice_total_amount")
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting._safe_fields", side_effect=lambda _doctype, fields: fields)
	@patch("qas_custom.services.school_admin_reporting._term_invoice_names", return_value=["SINV-001", "SINV-002", "SINV-003"])
	@patch("qas_custom.services.school_admin_reporting._validate_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_counts_only_submitted_paid_term_invoices_once(
		self,
		_mock_require,
		_mock_validate,
		_mock_names,
		_mock_fields,
		mock_get_all,
		mock_total,
		_mock_doctype,
	):
		mock_get_all.return_value = [
			frappe._dict(name="SINV-001", docstatus=1, status="Paid", rounded_total=540),
			frappe._dict(name="SINV-002", docstatus=1, status="Paid", rounded_total=68),
			# The service guard must not let a partly paid row through even if a
			# future query changes or a test double returns it.
			frappe._dict(name="SINV-003", docstatus=1, status="Partly Paid", rounded_total=100),
		]
		mock_total.side_effect = lambda row: row.get("rounded_total")

		result = get_school_admin_term_paid_invoice_summary_data("Term 3 2026")

		self.assertEqual(result, {
			"term": "Term 3 2026",
			"paid_invoice_count": 2,
			"paid_invoice_total": 608.0,
		})
		filters = mock_get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["name"], ["in", ["SINV-001", "SINV-002", "SINV-003"]])
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["status"], "Paid")

	@patch("qas_custom.services.school_admin_reporting._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	@patch("qas_custom.services.school_admin_reporting._term_invoice_names", return_value=[])
	@patch("qas_custom.services.school_admin_reporting._validate_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_empty_term_returns_zero_summary(self, _mock_require, _mock_validate, _mock_names, mock_get_all, _mock_doctype):
		result = get_school_admin_term_paid_invoice_summary_data("Term 3 2026")

		self.assertEqual(result["paid_invoice_count"], 0)
		self.assertEqual(result["paid_invoice_total"], 0.0)
		mock_get_all.assert_not_called()


class TestSchoolAdminReportingWeekday(TestCase):
	@patch("qas_custom.services.school_admin_reporting.frappe.get_all")
	def test_matches_any_students_regular_class_day_and_ignores_course_text(self, mock_get_all):
		mock_get_all.return_value = [
			{"name": "Wednesday-family", "student_details_json": json.dumps([
				{"class_schedule": [{"day_of_week": "Monday", "course": "Drawing"}]},
				{"class_schedule": [{"day_of_week": "Wednesday", "course": "Painting"}]},
			])},
			{"name": "Friday-family", "student_details_json": json.dumps([{"class_schedule": [{"day_of_week": "Friday", "course": "Wednesday art"}]}])},
			{"name": "Legacy-family", "student_details_json": json.dumps([{"student_name": "Amy"}])},
		]
		self.assertEqual(_family_row_names_for_weekday({"snapshot": "S1"}, "Wednesday"), ["Wednesday-family"])
		self.assertEqual(mock_get_all.call_args.kwargs["limit_page_length"], 0)

	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	@patch("qas_custom.services.school_admin_reporting._family_row_names_for_weekday", return_value=["R60"])
	def test_weekday_combines_with_outstanding_and_filters_before_pagination(self, mock_names, _require, _validate, mock_latest, _options):
		mock_latest.return_value = frappe._dict(name="S1", status="Completed")
		fake = SimpleNamespace(db=SimpleNamespace(count=Mock(return_value=1)), get_all=Mock(return_value=[]))
		with patch("qas_custom.services.school_admin_reporting.frappe", fake):
			result = get_school_admin_reporting_rows_data(term="Term 4 2026", report_type=FAMILY_REPORT_TYPE, invoice="Outstanding", day_of_week="Wednesday", page=2)
		filters = fake.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["invoice_classification"], "Outstanding")
		self.assertEqual(filters["name"], ["in", ["R60"]])
		self.assertEqual(fake.db.count.call_args.kwargs["filters"], filters)
		self.assertEqual(fake.get_all.call_args.kwargs["limit_start"], 50)
		self.assertEqual(result["total"], 1)


class TestSchoolAdminReportingExcel(TestCase):
	@patch("frappe.utils.xlsxutils.get_excel_date_format", return_value=("yyyy-mm-dd", "hh:mm:ss"))
	@patch("qas_custom.services.school_admin_reporting.get_school_admin_reporting_rows_data")
	def test_exports_all_pages_with_filters_numeric_amounts_and_safe_text(self, mock_rows, _date_format):
		from io import BytesIO
		from openpyxl import load_workbook
		row = {"parent_name": "=HYPERLINK(""https://example.test"")", "outstanding_amount": 100.5,
			"students": [{"student_name": "Amy", "class_schedule": [{"day_of_week": "Wednesday", "course": "Drawing"}]}]}
		snapshot = {"name": "S1", "completed_at": "2026-10-09 17:00:00"}
		mock_rows.side_effect = [{"snapshot": snapshot, "items": [row], "has_more": True}, {"snapshot": snapshot, "items": [dict(row, parent_name="Ben")], "has_more": False}]
		result = export_school_admin_reporting_families_data(term="Term 4 2026", invoice="Outstanding", day_of_week="Wednesday", query="Amy")
		book = load_workbook(BytesIO(result["content"]))
		self.assertTrue(result["filename"].endswith(".xlsx"))
		self.assertEqual(book.active.freeze_panes, "A2")
		self.assertEqual(book.active.auto_filter.ref, "A1:M3")
		self.assertTrue(book.active["E2"].alignment.wrap_text)
		self.assertEqual(book.active["M2"].value, 100.5)
		self.assertEqual(book.active["E2"].value, "Amy: Wednesday · Drawing")
		self.assertEqual(book.active["A2"].data_type, "s")
		self.assertEqual(book.active["A3"].value, "Ben")
		self.assertEqual(mock_rows.call_args_list[1].kwargs["page"], 2)
		for call in mock_rows.call_args_list:
			self.assertEqual(call.kwargs["day_of_week"], "Wednesday")
			self.assertEqual(call.kwargs["invoice"], "Outstanding")
			self.assertEqual(call.kwargs["query"], "Amy")


	@patch("qas_custom.services.school_admin_reporting.frappe.throw", side_effect=RuntimeError)
	@patch("qas_custom.services.school_admin_reporting.get_school_admin_reporting_rows_data")
	def test_rejects_an_export_if_report_changes_between_pages(self, mock_rows, _throw):
		mock_rows.side_effect = [
			{"snapshot": {"name": "S1"}, "items": [], "has_more": True},
			{"snapshot": {"name": "S2"}, "items": [], "has_more": False},
		]
		with self.assertRaises(RuntimeError):
			export_school_admin_reporting_families_data(term="Term 4 2026")


class TestSchoolAdminReportingRows(TestCase):
	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={"campuses": [], "teachers": []})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_unmarked_family_filter_includes_mixed_attendance(self, _require, _validate, mock_latest, _options):
		mock_latest.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Completed")
		fake_frappe = SimpleNamespace(db=SimpleNamespace(count=Mock(return_value=1)), get_all=Mock(return_value=[]))
		with patch("qas_custom.services.school_admin_reporting.frappe", fake_frappe):
			get_school_admin_reporting_rows_data(term="Term 3 2026", report_type=FAMILY_REPORT_TYPE, attendance="To be started")
		filters = fake_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["to_be_started_count"], [">", 0])
		self.assertNotIn("attendance_classification", filters)


	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={"campuses": [], "teachers": []})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_family_row_filters_are_applied_to_latest_snapshot(
		self,
		_mock_require,
		_mock_validate,
		mock_latest,
		_mock_options,
	):
		mock_latest.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Completed")
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(count=Mock(return_value=1)),
			get_all=Mock(return_value=[]),
		)
		with patch("qas_custom.services.school_admin_reporting.frappe", fake_frappe):
			result = get_school_admin_reporting_rows_data(
				term="Term 3 2026",
				report_type=FAMILY_REPORT_TYPE,
				attendance="Absent",
				invoice="Outstanding",
				query="pat",
			)
		filters = fake_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["snapshot"], "QARS-1")
		self.assertEqual(filters["attendance_classification"], "Absent")
		self.assertEqual(filters["invoice_classification"], "Outstanding")
		self.assertEqual(filters["search_text"], ["like", "%pat%"])
		self.assertEqual(result["total"], 1)

	@patch("qas_custom.services.school_admin_reporting._report_filter_options", return_value={"campuses": [], "teachers": []})
	@patch("qas_custom.services.school_admin_reporting._latest_completed_snapshot")
	@patch("qas_custom.services.school_admin_reporting._validate_reporting_term")
	@patch("qas_custom.services.school_admin_reporting._require_school_admin")
	def test_unmarked_filters_are_applied(
		self,
		_mock_require,
		_mock_validate,
		mock_latest,
		_mock_options,
	):
		mock_latest.return_value = frappe._dict(name="QARS-1", term="Term 3 2026", status="Completed")
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(count=Mock(return_value=0)),
			get_all=Mock(return_value=[]),
		)
		with patch("qas_custom.services.school_admin_reporting.frappe", fake_frappe):
			get_school_admin_reporting_rows_data(
				term="Term 3 2026",
				report_type=UNMARKED_REPORT_TYPE,
				campus="Indooroopilly",
				teacher="TEA-1",
			)
		filters = fake_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["campus"], "Indooroopilly")
		self.assertEqual(filters["teacher"], "TEA-1")
