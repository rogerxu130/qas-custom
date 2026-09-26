from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services import school_admin


class TestBulkInvoiceSubmitWorker(TestCase):
	def setUp(self):
		self.clock = patch.object(school_admin, "now_datetime", return_value=datetime(2026, 9, 26, 10, 0))
		self.clock.start()
		self.addCleanup(self.clock.stop)

	@patch.object(school_admin, "submit_school_admin_invoice_data")
	@patch.object(school_admin, "_", side_effect=lambda value: value)
	@patch.object(school_admin, "frappe")
	def test_one_invoice_waits_for_its_email_and_reports_failure(self, frappe_mock, translate, submit):
		frappe_mock.db.exists.return_value = True
		frappe_mock.db.get_value.return_value = 0
		submit.return_value = {"status": "Unpaid", "docstatus": 1, "notification": {"sent": False, "reason": "SMTP unavailable"}}
		row = school_admin._run_one_bulk_invoice_submit("SINV-1")
		submit.assert_called_once_with(invoice="SINV-1", enqueue_notification=False)
		self.assertTrue(row["ok"])
		self.assertIn("SMTP unavailable", row["message"])

	@patch.object(school_admin, "_get_latest_bulk_invoice_submit_job_status")
	@patch.object(school_admin, "frappe")
	@patch.object(school_admin, "_require_school_admin")
	def test_repeat_start_returns_active_job(self, require_admin, frappe_mock, latest):
		latest.return_value = {"job_id": "job-1", "status": "running", "processed": 1}
		result = school_admin.start_school_admin_bulk_invoice_submit_job_data({"all_drafts": True})
		self.assertEqual(result["job_id"], "job-1")
		frappe_mock.enqueue.assert_not_called()
