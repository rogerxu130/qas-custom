from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import maintenance
from qas_custom.services.inquiry import _build_webhook_response, assign_inquiry_course_session_core
from qas_custom.services.trial_invoice import (
	_check_rescheduled_trial_fee,
	_create_trial_invoice,
	_is_eligible,
	enqueue_trial_invoice_for_inquiry,
)


class TestTrialInvoiceAutomation(TestCase):
	def test_only_booked_or_rescheduled_trial_with_session_is_eligible(self):
		base = frappe._dict(inquiry_type="Trial Lesson", status="Booked", course_session="SESSION-001")
		self.assertTrue(_is_eligible(base))
		self.assertTrue(_is_eligible(frappe._dict(base, status="Rescheduled")))
		self.assertFalse(_is_eligible(frappe._dict(base, status="Needs Review")))
		self.assertFalse(_is_eligible(frappe._dict(base, inquiry_type="School Visit")))
		self.assertFalse(_is_eligible(frappe._dict(base, course_session=None)))

	@patch("qas_custom.services.trial_invoice.frappe.enqueue")
	@patch("qas_custom.services.trial_invoice.get_trial_invoice_status")
	def test_enqueue_uses_deduplicated_after_commit_job(self, mock_status, mock_enqueue):
		mock_status.return_value = {"trial_invoice_status": "queued"}
		inquiry = frappe._dict(name="INQ-0001")

		enqueue_trial_invoice_for_inquiry(inquiry)

		mock_enqueue.assert_called_once()
		kwargs = mock_enqueue.call_args.kwargs
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], "trial-invoice:INQ-0001")
		self.assertEqual(kwargs["inquiry"], "INQ-0001")

	@patch("qas_custom.services.trial_invoice.frappe.enqueue")
	@patch("qas_custom.services.trial_invoice.get_trial_invoice_status")
	def test_ineligible_inquiry_does_not_enqueue(self, mock_status, mock_enqueue):
		mock_status.return_value = {"trial_invoice_status": "skipped"}

		enqueue_trial_invoice_for_inquiry(frappe._dict(name="INQ-0001"))

		mock_enqueue.assert_not_called()

	@patch("qas_custom.services.trial_invoice.resolve_data_issue")
	@patch("qas_custom.services.trial_invoice._check_rescheduled_trial_fee")
	@patch("qas_custom.services.trial_invoice._link_invoice")
	@patch("qas_custom.services.trial_invoice._find_inquiry_invoice", return_value="SINV-0001")
	@patch("qas_custom.services.trial_invoice.frappe.get_doc")
	def test_existing_submitted_source_invoice_is_reused(self, mock_get_doc, _mock_find, mock_link, _mock_fee_check, mock_resolve):
		inquiry = frappe._dict(
			name="INQ-0001",
			inquiry_type="Trial Lesson",
			status="Booked",
			course_session="SESSION-001",
			trial_invoice=None,
		)
		invoice = frappe._dict(name="SINV-0001", docstatus=1, status="Unpaid")
		mock_get_doc.side_effect = [inquiry, invoice]
		with patch("qas_custom.services.trial_invoice.frappe.db", SimpleNamespace(commit=Mock())), patch(
			"qas_custom.services.trial_invoice._", side_effect=lambda value: value
		):
			result = _create_trial_invoice("INQ-0001")

		mock_link.assert_called_once_with("INQ-0001", "SINV-0001")
		mock_resolve.assert_called_once()
		self.assertEqual(result["trial_invoice"], "SINV-0001")
		self.assertEqual(result["trial_invoice_status"], "linked")

	@patch("qas_custom.services.school_admin.submit_school_admin_invoice_data")
	@patch("qas_custom.services.trial_invoice.resolve_data_issue")
	@patch("qas_custom.services.trial_invoice._link_invoice")
	@patch("qas_custom.services.trial_invoice._create_draft_trial_invoice", return_value="SINV-0002")
	@patch("qas_custom.services.trial_invoice._trial_invoice_context", return_value={})
	@patch("qas_custom.services.trial_invoice._find_inquiry_invoice", return_value=None)
	@patch("qas_custom.services.trial_invoice.frappe.get_doc")
	def test_new_invoice_is_linked_then_submitted_with_queued_notifications(
		self,
		mock_get_doc,
		_mock_find,
		_mock_context,
		_mock_create,
		mock_link,
		mock_resolve,
		mock_submit,
	):
		mock_get_doc.return_value = frappe._dict(
			name="INQ-0002",
			inquiry_type="Trial Lesson",
			status="Booked",
			course_session="SESSION-002",
			trial_invoice=None,
		)
		mock_submit.return_value = {"notification": {"queued": True}}

		with patch("qas_custom.services.trial_invoice.frappe.db", SimpleNamespace(commit=Mock())), patch(
			"qas_custom.services.trial_invoice._", side_effect=lambda value: value
		):
			result = _create_trial_invoice("INQ-0002")

		mock_link.assert_called_once_with("INQ-0002", "SINV-0002")
		mock_submit.assert_called_once_with(
			invoice="SINV-0002",
			enqueue_notification=True,
			send_notifications=True,
		)
		mock_resolve.assert_called_once()
		self.assertEqual(result["trial_invoice_status"], "linked")
		self.assertIn("notification was queued", result["trial_invoice_message"])

	@patch("qas_custom.services.trial_invoice.record_data_issue", return_value={"issue": "ISSUE-002"})
	@patch("qas_custom.services.trial_invoice._current_booking_fee", return_value=(60, "Advanced Art"))
	def test_reschedule_with_different_fee_creates_manual_review_issue(self, _mock_fee, mock_record):
		inquiry = frappe._dict(name="INQ-0004", student="STU-001", course_session="SESSION-004")
		invoice = frappe._dict(name="SINV-0004", items=[frappe._dict(qty=1, rate=45, amount=45)])

		with patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			_check_rescheduled_trial_fee(inquiry, invoice)

		issue = mock_record.call_args.args[0]
		self.assertEqual(issue["issue_type"], "Billing Configuration")
		self.assertEqual(issue["related_document"], "SINV-0004")
		self.assertIn("Advanced Art", issue["description"])

	@patch("qas_custom.services.inquiry.build_inquiry_detail", return_value={"inquiry": {"id": "INQ-0003"}})
	@patch("qas_custom.services.inquiry.enqueue_trial_invoice_for_inquiry")
	@patch("qas_custom.services.inquiry.frappe.get_doc")
	def test_assigning_course_session_queues_trial_invoice(self, mock_get_doc, mock_enqueue, _mock_detail):
		doc = frappe._dict(
			name="INQ-0003",
			inquiry_type="Trial Lesson",
			status="Needs Review",
			course_session=None,
		)
		doc.save = Mock()
		mock_get_doc.return_value = doc

		with patch("qas_custom.services.inquiry.frappe.db", SimpleNamespace(commit=Mock())):
			assign_inquiry_course_session_core("INQ-0003", "SESSION-003")

		self.assertEqual(doc.status, "Booked")
		self.assertEqual(doc.course_session, "SESSION-003")
		mock_enqueue.assert_called_once_with(doc)

	@patch("qas_custom.services.inquiry.build_inquiry_detail")
	def test_webhook_response_exposes_queued_invoice_status(self, mock_detail):
		mock_detail.return_value = {"inquiry": {
			"id": "INQ-0005",
			"status": "Booked",
			"course_session": "SESSION-005",
			"trial_invoice": None,
			"trial_invoice_status": "queued",
			"trial_invoice_message": "Trial Invoice creation is queued.",
		}}

		result = _build_webhook_response("INQ-0005", status="created", duplicate=False)

		self.assertEqual(result["trial_invoice_status"], "queued")
		self.assertEqual(result["trial_invoice_message"], "Trial Invoice creation is queued.")


class TestCourseTrialFeeMaintenance(TestCase):
	@patch.object(maintenance, "get_trial_class_fee", return_value=0)
	@patch.object(maintenance, "get_trial_class_fee_field", return_value="pay_as_you_go_fee")
	@patch.object(maintenance, "_notify_school_admins_of_new_issues")
	@patch.object(maintenance, "_upsert_data_issue", return_value=("ISSUE-001", True))
	@patch.object(maintenance, "_resolve_data_issue")
	@patch.object(maintenance, "_has_field")
	@patch.object(maintenance, "_doctype_available", return_value=True)
	@patch.object(maintenance.frappe, "get_all")
	def test_active_course_without_fee_creates_billing_issue(
		self,
		mock_get_all,
		_mock_doctype,
		mock_has_field,
		_mock_resolve,
		mock_upsert,
		mock_notify,
		_mock_fee_field,
		_mock_fee,
	):
		mock_has_field.side_effect = lambda doctype, field: field in {"status", "pay_as_you_go_fee"}
		mock_get_all.return_value = [frappe._dict(name="Creative Art", status="Active", pay_as_you_go_fee=0)]

		with patch.object(maintenance.frappe, "db", SimpleNamespace(commit=Mock())), patch.object(
			maintenance, "_", side_effect=lambda value: value
		):
			result = maintenance.reconcile_course_trial_fees()

		issue = mock_upsert.call_args.args[0]
		self.assertEqual(issue["issue_type"], "Billing Configuration")
		self.assertEqual(issue["source_doctype"], "Course")
		self.assertEqual(issue["source_document"], "Creative Art")
		self.assertEqual(result["invalid_courses"], ["Creative Art"])
		mock_notify.assert_called_once_with(["ISSUE-001"])

	@patch.object(maintenance, "reconcile_course_trial_fees", return_value={"checked": 4})
	@patch.object(maintenance, "reconcile_attendance_links", return_value={"issues_seen": 0})
	@patch.object(maintenance, "sync_student_activity_status", return_value={"checked": 10})
	def test_nightly_maintenance_includes_trial_fee_check(self, _mock_students, _mock_attendance, _mock_fees):
		result = maintenance.run_nightly_maintenance()

		self.assertEqual(result["course_trial_fees"], {"checked": 4})
