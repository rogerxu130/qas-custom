from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

from frappe import _dict

from qas_custom.services import inquiry


class TestDuplicateTrialIntakeReview(TestCase):
	def create_trial(self, existing):
		doc = _dict(
			name="INQ-NEW", flags=_dict(), review_reason=None,
			meta=_dict(has_field=lambda field: True), is_new=lambda: True,
		)
		doc.set = lambda field, value: doc.__setitem__(field, value)
		context = {
			"session": {"name": "SESSION-001", "session_date": "2026-10-10"},
			"timeslot": {"course": "Art", "start_time": "10:00:00"},
			"campus": "Campus",
		}
		payload = {
			"inquiry_type": "Trial Lesson", "course_session": "SESSION-001",
			"external_submission_id": "fluent:123", "skip_confirmation": True,
			"submitted_student_name": "Student Name", "raw_webhook_payload": {"submission_id": "123"},
		}
		with ExitStack() as stack:
			def mock(name, **kwargs):
				return stack.enter_context(patch("qas_custom.services.inquiry." + name, **kwargs))
			mock("_", side_effect=lambda text: text)
			mock("_normalize_inquiry_payload", side_effect=lambda data: data)
			mock("_resolve_parent", return_value="PARENT-001")
			mock("_resolve_student", return_value="STUDENT-001")
			mock("_resolve_trial_session_context", return_value=(context, None))
			mock("_get_session_context", return_value=context)
			mock("get_student_session_attendance_entry", return_value=existing)
			mock("frappe.new_doc", return_value=doc)
			mock("_set_special_needs_on_inquiry")
			mock("_sync_student_special_needs")
			invoice = mock("enqueue_trial_invoice_for_inquiry")
			alert = mock("_send_needs_review_alert")
			db = mock("frappe.db", new=_dict(commit=Mock()))
			commit = db.commit
			mock("build_inquiry_detail", side_effect=lambda name: {"inquiry": {"id": name, "status": doc.status}})
			attendance = stack.enter_context(patch("qas_custom.modules.attendance.commands.create_attendance_entry"))
			stack.enter_context(patch("qas_custom.modules.attendance.commands.get_attendance_entry_by_source", return_value=None))

			def insert():
				inquiry.sync_inquiry_course_session(doc)
				inquiry.ensure_inquiry_attendance_entry(doc)

			doc.insert = Mock(side_effect=insert)
			result = inquiry.create_inquiry_core(payload, source="Fluent Form")
		return doc, result, attendance, alert, invoice, commit

	def test_conflicting_intake_is_saved_for_review_without_attendance(self):
		for enrollment_type, status in [("Trial", "To be started"), ("Full-Term", "To be started"), ("Full-Term", "Cancelled")]:
			with self.subTest(enrollment_type=enrollment_type, status=status):
				doc, result, attendance, alert, invoice, commit = self.create_trial(
					{"name": "ATT-EXISTING", "status": status, "enrollment_type": enrollment_type}
				)
				self.assertEqual(result["inquiry"]["status"], "Needs Review")
				self.assertEqual(doc.student, "STUDENT-001")
				self.assertEqual(doc.external_submission_id, "fluent:123")
				self.assertEqual(doc.submitted_student_name, "Student Name")
				self.assertIn('"submission_id": "123"', doc.raw_webhook_payload)
				self.assertIsNone(doc.course_session)
				self.assertEqual(doc.campus, "Campus")
				self.assertEqual(doc.preferred_course, "Art")
				self.assertEqual(doc.current_appointment_date, "2026-10-10")
				self.assertEqual(doc.current_appointment_time, "10:00:00")
				self.assertEqual(doc.confirmation_status, "Not Required")
				self.assertIn("SESSION-001", doc.review_reason)
				self.assertIn("ATT-EXISTING", doc.review_reason)
				attendance.assert_not_called()
				alert.assert_called_once_with(doc, doc.review_reason)
				commit.assert_called_once()
				self.assertEqual(invoice.call_args.args[0].status, "Needs Review")

	def test_available_session_still_books_normally(self):
		doc, result, attendance, alert, _invoice, _commit = self.create_trial(None)
		self.assertEqual(result["inquiry"]["status"], "Booked")
		self.assertEqual(doc.course_session, "SESSION-001")
		attendance.assert_called_once()
		alert.assert_not_called()

	def test_cancelled_trial_can_still_be_reactivated(self):
		doc, _result, attendance, alert, _invoice, _commit = self.create_trial(
			{"name": "ATT-OLD", "status": "Cancelled", "enrollment_type": "Trial"}
		)
		self.assertEqual(doc.status, "Booked")
		self.assertEqual(doc.course_session, "SESSION-001")
		self.assertTrue(attendance.call_args.kwargs["reactivate_cancelled_duplicate"])
		alert.assert_not_called()

	def test_manual_assignment_to_conflicting_session_still_fails(self):
		doc = _dict(
			name="INQ-REVIEW", inquiry_type="Trial Lesson", status="Needs Review",
			student="STUDENT-001", course_session=None, review_reason="Duplicate request",
			is_new=lambda: False,
			get_doc_before_save=lambda: _dict(course_session=None, status="Needs Review"),
		)
		doc.save = Mock(side_effect=lambda **kwargs: inquiry.sync_inquiry_course_session(doc))
		with (
			patch.object(inquiry.frappe, "get_doc", return_value=doc),
			patch.object(inquiry, "_get_session_context", return_value={"session": {"name": "SESSION-001"}, "timeslot": {}, "campus": "Campus"}),
			patch("qas_custom.modules.attendance.commands.get_attendance_entry_by_source", return_value=None),
			patch("qas_custom.services.class_attendance.get_student_session_attendance_entry", return_value={"name": "ATT-OLD", "status": "To be started", "enrollment_type": "Trial"}),
			patch("qas_custom.services.class_attendance._", side_effect=lambda text: text),
			patch("qas_custom.services.class_attendance.frappe.throw", side_effect=RuntimeError("This student is already listed for this session.")),
			patch.object(inquiry, "enqueue_trial_invoice_for_inquiry") as invoice,
			patch.object(inquiry.frappe, "db", new=_dict(commit=Mock())) as db,
		):
			with self.assertRaisesRegex(RuntimeError, "already listed"):
				inquiry.assign_inquiry_course_session_core(doc.name, "SESSION-001")
			invoice.assert_not_called()
			db.commit.assert_not_called()

	def test_same_external_submission_reuses_saved_inquiry(self):
		with (
			patch.object(inquiry, "_get_payload", return_value={}),
			patch.object(inquiry, "_validate_webhook_token"),
			patch.object(inquiry, "_normalize_webhook_payload", return_value={"external_submission_id": "fluent:123"}),
			patch.object(inquiry, "_get_existing_webhook_inquiry", return_value="INQ-REVIEW"),
			patch.object(inquiry, "_build_webhook_response", return_value={"status": "duplicate", "duplicate": True}) as response,
			patch.object(inquiry, "create_inquiry_core") as create,
		):
			self.assertTrue(inquiry.create_inquiry_webhook_data({})["duplicate"])
			response.assert_called_once_with("INQ-REVIEW", status="duplicate", duplicate=True)
			create.assert_not_called()

	def test_review_can_be_cancelled_without_touching_existing_booking(self):
		doc = _dict(
			name="INQ-REVIEW", inquiry_type="Trial Lesson", status="Needs Review",
			student="STUDENT-001", course_session=None,
			is_new=lambda: False,
			get_doc_before_save=lambda: _dict(course_session=None, status="Needs Review"),
		)
		doc.save = Mock(side_effect=lambda **kwargs: inquiry.sync_inquiry_course_session(doc))
		with (
			patch.object(inquiry.frappe, "get_doc", return_value=doc),
			patch.object(inquiry.frappe, "db", new=_dict(commit=Mock())),
			patch.object(inquiry, "_cancel_linked_trial_invoice_for_cancelled_inquiry"),
			patch.object(inquiry, "cancel_trial_inquiry_attendance_entries") as cancel,
			patch.object(inquiry, "build_inquiry_detail", return_value={"inquiry": {"status": "Cancelled"}}),
		):
			result = inquiry.mark_inquiry_status_core(doc.name, "Cancelled")
			self.assertEqual(result["inquiry"]["status"], "Cancelled")
			self.assertIsNone(doc.course_session)
			cancel.assert_called_once_with("INQ-REVIEW")

	def test_review_can_be_assigned_to_another_session(self):
		doc = _dict(
			name="INQ-REVIEW", inquiry_type="Trial Lesson", status="Needs Review",
			student="STUDENT-001", course_session=None, review_reason="Duplicate request",
			is_new=lambda: False,
			get_doc_before_save=lambda: _dict(course_session=None, status="Needs Review"),
		)
		doc.save = Mock(side_effect=lambda **kwargs: inquiry.sync_inquiry_course_session(doc))
		with (
			patch.object(inquiry.frappe, "get_doc", return_value=doc),
			patch.object(inquiry.frappe, "db", new=_dict(commit=Mock())),
			patch.object(inquiry, "_get_session_context", return_value={"session": {"name": "SESSION-002"}, "timeslot": {}, "campus": "Campus"}),
			patch.object(inquiry, "ensure_inquiry_attendance_entry") as attendance,
			patch.object(inquiry, "enqueue_trial_invoice_for_inquiry"),
			patch.object(inquiry, "build_inquiry_detail", return_value={"inquiry": {"status": "Booked"}}),
		):
			inquiry.assign_inquiry_course_session_core(doc.name, "SESSION-002")
			self.assertEqual(doc.status, "Booked")
			self.assertEqual(doc.course_session, "SESSION-002")
			self.assertIsNone(doc.review_reason)
			attendance.assert_called_once_with(doc)

	def test_make_receives_success_with_review_required(self):
		with patch.object(inquiry, "build_inquiry_detail", return_value={"inquiry": {
			"id": "INQ-REVIEW", "status": "Needs Review", "course_session": None,
			"review_reason": "Student already listed for SESSION-001",
		}}):
			response = inquiry._build_webhook_response("INQ-REVIEW", status="created", duplicate=False)
		self.assertEqual(response["status"], "created")
		self.assertEqual(response["inquiry_status"], "Needs Review")
		self.assertTrue(response["review_required"])
		self.assertIsNone(response["course_session"])
		self.assertIn("SESSION-001", response["review_reason"])
