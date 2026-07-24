from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.services.term_overdue_invoice_reminders import (
	_require_school_admin,
	_term_invoice_names,
	get_term_overdue_invoice_reminder_preview_data,
	run_term_overdue_invoice_reminder_job,
	start_term_overdue_invoice_reminder_job_data,
)


def invoice(name, **overrides):
	values = {
		"name": name,
		"customer": "Customer One",
		"parent": "Parent One",
		"due_date": "2026-07-01",
		"outstanding_amount": 120,
		"grand_total": 120,
		"docstatus": 1,
		"status": "Overdue",
		"is_return": 0,
	}
	values.update(overrides)
	return frappe._dict(values)


class TestTermOverdueInvoicePreview(TestCase):
	@patch("qas_custom.services.term_overdue_invoice_reminders.frappe.get_all")
	def test_term_invoice_names_are_deduplicated(self, mock_get_all):
		mock_get_all.return_value = ["INV-002", "INV-001", "INV-002"]
		self.assertEqual(_term_invoice_names("Term 3 2026"), ["INV-002", "INV-001"])

	@patch(
		"qas_custom.modules.notifications.invoice_overdue_reminders._system_datetime_to_brisbane",
		side_effect=lambda value: value,
	)
	@patch("qas_custom.services.term_overdue_invoice_reminders._invoice_recipient")
	@patch("qas_custom.services.term_overdue_invoice_reminders._get_reminder_history")
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_item_rows")
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_rows")
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_names")
	@patch("qas_custom.services.term_overdue_invoice_reminders._brisbane_date", return_value=date(2026, 7, 24))
	@patch("qas_custom.services.term_overdue_invoice_reminders._validate_term", return_value="Term 3 2026")
	@patch("qas_custom.services.term_overdue_invoice_reminders._require_school_admin")
	def test_preview_includes_only_cadence_eligible_invoices(
		self,
		_mock_require,
		_mock_validate,
		_mock_today,
		mock_names,
		mock_rows,
		mock_items,
		mock_history,
		mock_recipient,
		_mock_timezone,
	):
		mock_names.return_value = ["INV-001", "INV-002"]
		mock_rows.return_value = [invoice("INV-001"), invoice("INV-002", outstanding_amount=80)]
		mock_items.return_value = [
			frappe._dict(parent="INV-001", student_display_name="Student One"),
			frappe._dict(parent="INV-001", student_display_name="Student One"),
			frappe._dict(parent="INV-002", student_display_name="Student Two"),
		]
		mock_history.return_value = {
			"INV-001": [],
			"INV-002": [{"creation": datetime(2026, 7, 23, 9, 0)}],
		}
		mock_recipient.return_value = {
			"email": "parent@example.com",
			"parent": "Parent One",
			"customer": "Customer One",
		}

		result = get_term_overdue_invoice_reminder_preview_data("Term 3 2026")

		self.assertEqual(result["matching_invoice_count"], 2)
		self.assertEqual(result["eligible_invoice_count"], 1)
		self.assertEqual(result["unique_parent_count"], 1)
		self.assertEqual(result["total_outstanding"], 120)
		self.assertEqual(result["excluded_counts"], {"recently_reminded": 1})
		self.assertEqual(result["items"][0]["invoice"], "INV-001")
		self.assertEqual(result["items"][0]["students"], ["Student One"])

	@patch("qas_custom.services.term_overdue_invoice_reminders._invoice_recipient", return_value={"email": None})
	@patch("qas_custom.services.term_overdue_invoice_reminders._get_reminder_history", return_value={"INV-001": []})
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_item_rows", return_value=[])
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_rows", return_value=[invoice("INV-001")])
	@patch("qas_custom.services.term_overdue_invoice_reminders._term_invoice_names", return_value=["INV-001"])
	@patch("qas_custom.services.term_overdue_invoice_reminders._brisbane_date", return_value=date(2026, 7, 24))
	@patch("qas_custom.services.term_overdue_invoice_reminders._validate_term", return_value="Term 3 2026")
	@patch("qas_custom.services.term_overdue_invoice_reminders._require_school_admin")
	def test_preview_excludes_missing_recipient(
		self,
		_mock_require,
		_mock_validate,
		_mock_today,
		_mock_names,
		_mock_rows,
		_mock_items,
		_mock_history,
		_mock_recipient,
	):
		result = get_term_overdue_invoice_reminder_preview_data("Term 3 2026")
		self.assertEqual(result["eligible_invoice_count"], 0)
		self.assertEqual(result["excluded_counts"], {"missing_recipient": 1})


class TestTermOverdueInvoiceJob(TestCase):
	@patch("qas_custom.services.term_overdue_invoice_reminders.frappe.enqueue")
	@patch("qas_custom.services.term_overdue_invoice_reminders._set_job_status")
	@patch("qas_custom.services.term_overdue_invoice_reminders.frappe.generate_hash", return_value="JOB-001")
	@patch("qas_custom.services.term_overdue_invoice_reminders.get_term_overdue_invoice_reminder_preview_data")
	@patch("qas_custom.services.term_overdue_invoice_reminders._validate_term", return_value="Term 3 2026")
	@patch("qas_custom.services.term_overdue_invoice_reminders._require_school_admin")
	def test_start_job_uses_server_preview_invoice_scope(
		self,
		_mock_require,
		_mock_validate,
		mock_preview,
		_mock_hash,
		mock_set_status,
		mock_enqueue,
	):
		mock_preview.return_value = {
			"items": [{"invoice": "INV-001"}, {"invoice": "INV-002"}],
			"unique_parent_count": 2,
			"total_outstanding": 200,
		}
		with patch(
			"qas_custom.services.term_overdue_invoice_reminders.frappe.session",
			SimpleNamespace(user="school@example.com"),
		):
			result = start_term_overdue_invoice_reminder_job_data({"term": "Term 3 2026", "invoices": ["IGNORED"]})

		self.assertEqual(result["job_id"], "JOB-001")
		self.assertEqual(result["total"], 2)
		mock_set_status.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs["invoices"], ["INV-001", "INV-002"])

	@patch("qas_custom.services.term_overdue_invoice_reminders._set_job_status")
	@patch("qas_custom.services.term_overdue_invoice_reminders._get_job_status", return_value=None)
	@patch("qas_custom.services.term_overdue_invoice_reminders.queue_overdue_invoice_reminder")
	@patch(
		"qas_custom.services.term_overdue_invoice_reminders.now_datetime",
		return_value=datetime(2026, 7, 24, 9, 0),
	)
	def test_job_tracks_queued_and_cadence_skipped_invoices(
		self,
		_mock_now,
		mock_queue,
		_mock_get_status,
		mock_set_status,
	):
		mock_queue.side_effect = [
			{"queued": True, "notification_log": "LOG-001"},
			{"queued": False, "skipped": True, "reason_code": "recently_reminded", "reason": "Recently reminded"},
		]
		database = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
		with patch("qas_custom.services.term_overdue_invoice_reminders.frappe.db", database):
			result = run_term_overdue_invoice_reminder_job(
				qas_job_id="JOB-001",
				term="Term 3 2026",
				invoices=["INV-001", "INV-002"],
			)

		self.assertEqual(result["status"], "completed")
		self.assertEqual(result["processed"], 2)
		self.assertEqual(result["queued"], 1)
		self.assertEqual(result["skipped"], 1)
		self.assertEqual(result["failed"], 0)
		self.assertGreaterEqual(mock_set_status.call_count, 4)


class TestTermOverdueInvoicePermissions(TestCase):
	@patch("qas_custom.services.term_overdue_invoice_reminders.frappe.throw", side_effect=frappe.PermissionError)
	@patch("qas_custom.services.term_overdue_invoice_reminders.frappe.get_roles", return_value=["Teacher"])
	def test_school_admin_role_is_required(self, _mock_roles, _mock_throw):
		with patch(
			"qas_custom.services.term_overdue_invoice_reminders.frappe.session",
			SimpleNamespace(user="teacher@example.com"),
		):
			with self.assertRaises(frappe.PermissionError):
				_require_school_admin()
