from unittest import TestCase
from unittest.mock import patch
from datetime import datetime

from qas_custom.services import term_generation_jobs as jobs


def sample_status(operation):
	return {
		"job_id": "job-1", "term": "TERM-4", "operation": operation, "status": "queued",
		"total": 2, "processed": 0, "succeeded": 0, "skipped": 0, "failed": 0,
		"activated_enrollments": 0, "attendance_entries": 0,
		"created_invoices": 0, "invoice_items": 0,
		"current_enrollment": None, "error_rows": [], "warnings": [],
	}


class TestTermGenerationJobs(TestCase):
	def setUp(self):
		self.clock = patch.object(jobs, "now_datetime", return_value=datetime(2026, 9, 26, 10, 0))
		self.clock.start()
		self.addCleanup(self.clock.stop)

	@patch.object(jobs, "_get_status")
	@patch.object(jobs, "frappe")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_repeat_start_returns_running_job_without_queueing_again(self, require_admin, frappe_mock, get_status):
		get_status.side_effect = lambda term, operation: sample_status("attendance") if operation == "attendance" else None
		frappe_mock.db.exists.return_value = True
		result = jobs.start_school_admin_term_generation_job_data("TERM-4", "attendance")
		self.assertEqual(result["job_id"], "job-1")
		frappe_mock.enqueue.assert_not_called()

	@patch.object(jobs, "_get_status")
	@patch.object(jobs, "_set_status")
	@patch.object(jobs, "frappe")
	@patch("qas_custom.services.school_admin._create_attendance_for_enrollment_names")
	def test_attendance_commits_each_enrollment_and_reports_progress(self, generate, db, set_status, get_status):
		status = sample_status("attendance")
		get_status.return_value = status
		generate.side_effect = [
			{"activated_enrollments": 1, "attendance_entries": 9, "skipped": 0, "errors": 0},
			{"activated_enrollments": 1, "attendance_entries": 8, "skipped": 0, "errors": 0},
		]
		result = jobs.run_school_admin_term_generation_job("job-1", "TERM-4", "attendance", ["ENR-1", "ENR-2"])
		self.assertEqual(result["status"], "completed")
		self.assertEqual(result["processed"], 2)
		self.assertEqual(result["attendance_entries"], 17)
		self.assertEqual(result["activated_enrollments"], 2)
		self.assertEqual(db.db.commit.call_count, 2)
		self.assertGreaterEqual(set_status.call_count, 4)

	@patch.object(jobs, "_get_status")
	@patch.object(jobs, "_set_status")
	@patch.object(jobs, "frappe")
	@patch("qas_custom.services.school_admin._create_invoices_for_enrollment_names")
	def test_invoice_error_does_not_stop_other_enrollments(self, generate, db, set_status, get_status):
		status = sample_status("invoices")
		get_status.return_value = status
		generate.side_effect = [
			{"created_invoices": 1, "invoice_items": 1, "skipped": 0, "errors": 0},
			{"created_invoices": 0, "invoice_items": 0, "skipped": 0, "errors": 1,
			 "error_rows": [{"enrollment": "ENR-2", "error": "Missing fee"}]},
		]
		result = jobs.run_school_admin_term_generation_job("job-1", "TERM-4", "invoices", ["ENR-1", "ENR-2"])
		self.assertEqual(result["status"], "completed_with_errors")
		self.assertEqual(result["processed"], 2)
		self.assertEqual(result["created_invoices"], 1)
		self.assertEqual(result["failed"], 1)
		self.assertEqual(result["error_rows"][0]["enrollment"], "ENR-2")
		self.assertEqual(db.db.commit.call_count, 2)
