from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import maintenance
from qas_custom.services.inquiry import _build_webhook_response, assign_inquiry_course_session_core
from qas_custom.services.trial_invoice import (
	_check_rescheduled_trial_fee,
	_classify_replacement_invoices,
	_create_replacement_trial_invoice_draft,
	_create_trial_invoice,
	_is_eligible,
	create_replacement_trial_invoice_draft,
	enqueue_trial_invoice_for_inquiry,
	preview_replacement_trial_invoice,
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

	@patch("qas_custom.services.school_admin.submit_school_admin_invoice_data")
	@patch("qas_custom.services.trial_invoice._link_invoice")
	@patch("qas_custom.services.trial_invoice._find_inquiry_invoice", return_value="SINV-REPLACEMENT")
	@patch("qas_custom.services.trial_invoice.frappe.get_doc")
	def test_replacement_draft_is_never_automatically_submitted(
		self,
		mock_get_doc,
		_mock_find,
		mock_link,
		mock_submit,
	):
		inquiry = frappe._dict(
			name="INQ-REPLACEMENT",
			inquiry_type="Trial Lesson",
			status="Rescheduled",
			course_session="SESSION-REPLACEMENT",
			trial_invoice=None,
		)
		invoice = frappe._dict(
			name="SINV-REPLACEMENT",
			docstatus=0,
			status="Draft",
			source_type="Replacement Trial Inquiry",
		)
		mock_get_doc.side_effect = [inquiry, invoice]

		with patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			result = _create_trial_invoice("INQ-REPLACEMENT")

		mock_link.assert_called_once_with("INQ-REPLACEMENT", "SINV-REPLACEMENT")
		mock_submit.assert_not_called()
		self.assertEqual(result["trial_invoice_status"], "queued")
		self.assertIn("waiting for School Admin review", result["trial_invoice_message"])

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

	def test_replacement_is_blocked_until_active_invoice_is_cancelled(self):
		inquiry = frappe._dict(name="INQ-0100", trial_invoice="SINV-0100")
		rows = [
			frappe._dict(
				name="SINV-0100",
				docstatus=1,
				status="Unpaid",
				grand_total=85,
				creation="2026-07-23 10:00:00",
			)
		]

		with patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			result = _classify_replacement_invoices(inquiry, rows)

		self.assertEqual(result["state"], "blocked")
		self.assertEqual(result["invoice"]["name"], "SINV-0100")
		self.assertIn("Cancel it manually", result["message"])

	def test_cancelled_invoice_allows_replacement_draft(self):
		inquiry = frappe._dict(name="INQ-0101", trial_invoice="SINV-0101")
		rows = [
			frappe._dict(
				name="SINV-0101",
				docstatus=2,
				status="Cancelled",
				grand_total=85,
				creation="2026-07-23 10:00:00",
			)
		]

		with patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			result = _classify_replacement_invoices(inquiry, rows)

		self.assertEqual(result["state"], "ready")
		self.assertEqual(result["invoice"]["name"], "SINV-0101")

	def test_existing_replacement_draft_is_reused(self):
		inquiry = frappe._dict(name="INQ-0102", trial_invoice="SINV-OLD")
		rows = [
			frappe._dict(name="SINV-NEW", docstatus=0, status="Draft", grand_total=68, creation="2026-07-23 11:00:00"),
			frappe._dict(name="SINV-OLD", docstatus=2, status="Cancelled", grand_total=85, creation="2026-07-23 10:00:00"),
		]

		with patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			result = _classify_replacement_invoices(inquiry, rows)

		self.assertEqual(result["state"], "existing_draft")
		self.assertEqual(result["invoice"]["name"], "SINV-NEW")

	@patch("qas_custom.services.trial_invoice._replacement_trial_invoice_context")
	@patch("qas_custom.services.trial_invoice._inquiry_invoice_rows")
	@patch("qas_custom.services.trial_invoice._replacement_inquiry_doc")
	def test_replacement_preview_uses_current_inquiry_booking(self, mock_inquiry, mock_rows, mock_context):
		mock_inquiry.return_value = frappe._dict(
			name="INQ-0103",
			trial_invoice="SINV-OLD",
			student="STU-0103",
			course_session="SESSION-0103",
		)
		mock_rows.return_value = [
			frappe._dict(name="SINV-OLD", docstatus=2, status="Cancelled", grand_total=85, creation="2026-07-23 10:00:00")
		]
		mock_context.return_value = {
			"session": frappe._dict(session_date="2026-07-25"),
			"timeslot": frappe._dict(start_time="10:40:00", end_time="12:40:00"),
			"course": "Anime Art - Intermediate",
			"campus": "Upper Mount Gravatt",
			"fee": 68,
		}

		with patch("qas_custom.services.trial_invoice.get_student_parent_name", return_value="Daniel SPIRIG"), patch(
			"qas_custom.services.trial_invoice.get_course_session_snapshot_label",
			return_value="Saturday 10:40 - Anime Art",
		), patch("qas_custom.services.trial_invoice._", side_effect=lambda value: value):
			result = preview_replacement_trial_invoice("INQ-0103")

		self.assertTrue(result["can_create"])
		self.assertEqual(result["current_invoice"]["name"], "SINV-OLD")
		self.assertEqual(result["replacement"]["course"], "Anime Art - Intermediate")
		self.assertEqual(result["replacement"]["trial_fee"], 68)

	@patch("qas_custom.services.trial_invoice.preview_replacement_trial_invoice")
	@patch("qas_custom.services.trial_invoice._link_invoice")
	def test_create_replacement_returns_existing_draft_without_duplication(self, mock_link, mock_preview):
		mock_preview.return_value = {
			"state": "existing_draft",
			"existing_draft": {"name": "SINV-DRAFT", "docstatus": 0, "status": "Draft", "amount": 68},
		}
		with patch("qas_custom.services.trial_invoice.frappe.db", SimpleNamespace(commit=Mock())), patch(
			"qas_custom.services.trial_invoice._",
			side_effect=lambda value: value,
		):
			result = _create_replacement_trial_invoice_draft("INQ-0104")

		self.assertFalse(result["created"])
		self.assertEqual(result["invoice"]["name"], "SINV-DRAFT")
		mock_link.assert_called_once_with("INQ-0104", "SINV-DRAFT")

	@patch("qas_custom.services.trial_invoice._create_replacement_trial_invoice_draft")
	def test_create_replacement_uses_inquiry_lock(self, mock_create):
		mock_create.return_value = {"created": True}
		lock = Mock(return_value=nullcontext())
		with patch("qas_custom.services.trial_invoice.frappe.cache", SimpleNamespace(lock=lock)):
			result = create_replacement_trial_invoice_draft("INQ-0105")

		self.assertTrue(result["created"])
		lock.assert_called_once_with(
			"qas-replacement-trial-invoice:INQ-0105",
			timeout=60,
			blocking_timeout=10,
		)

	@patch("qas_custom.services.trial_invoice.resolve_data_issue")
	@patch("qas_custom.services.trial_invoice._record_replacement_trial_invoice_audit")
	@patch("qas_custom.services.trial_invoice._link_invoice")
	@patch("qas_custom.services.trial_invoice._create_draft_trial_invoice", return_value="SINV-NEW")
	@patch("qas_custom.services.trial_invoice._classify_replacement_invoices")
	@patch("qas_custom.services.trial_invoice._inquiry_invoice_rows")
	@patch("qas_custom.services.trial_invoice._replacement_trial_invoice_context")
	@patch("qas_custom.services.trial_invoice._replacement_inquiry_doc")
	@patch("qas_custom.services.trial_invoice.preview_replacement_trial_invoice")
	def test_create_replacement_links_new_draft_and_resolves_fee_issue(
		self,
		mock_preview,
		mock_inquiry,
		mock_context,
		mock_rows,
		mock_classify,
		mock_create,
		mock_link,
		mock_audit,
		mock_resolve,
	):
		old_invoice = frappe._dict(name="SINV-OLD", docstatus=2, status="Cancelled")
		mock_preview.return_value = {"state": "ready"}
		mock_inquiry.return_value = frappe._dict(name="INQ-0106")
		mock_context.return_value = {"fee": 68}
		mock_rows.return_value = [old_invoice]
		mock_classify.return_value = {"state": "ready", "invoice": old_invoice}
		db = SimpleNamespace(
			savepoint=Mock(),
			commit=Mock(),
			rollback=Mock(),
			get_value=Mock(return_value=frappe._dict(
				name="SINV-NEW",
				docstatus=0,
				status="Draft",
				grand_total=68,
				rounded_total=68,
				outstanding_amount=0,
			)),
		)

		with patch("qas_custom.services.trial_invoice.frappe.db", db), patch(
			"qas_custom.services.trial_invoice._",
			side_effect=lambda value: value,
		):
			result = _create_replacement_trial_invoice_draft("INQ-0106")

		self.assertTrue(result["created"])
		self.assertEqual(result["invoice"]["name"], "SINV-NEW")
		mock_create.assert_called_once_with(mock_inquiry.return_value, mock_context.return_value, replacement=True)
		mock_link.assert_called_once_with("INQ-0106", "SINV-NEW")
		mock_audit.assert_called_once_with(mock_inquiry.return_value, old_invoice, "SINV-NEW")
		self.assertEqual(mock_resolve.call_count, 2)
		db.commit.assert_called_once()
		db.rollback.assert_not_called()

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
