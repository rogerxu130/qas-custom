from unittest import TestCase
from unittest.mock import MagicMock, patch

from qas_custom.modules.trial_referrals import is_referral_claim
from qas_custom.services.inquiry import (
	_normalize_inquiry_payload,
	_normalize_school_visit_webhook_payload,
	_resolve_student,
	_submission_data_rows,
	create_school_visit_webhook_data,
)


class TestInquirySubmissionData(TestCase):
	@patch("qas_custom.services.inquiry.frappe.new_doc")
	@patch("qas_custom.services.inquiry._validate_exists")
	def test_explicit_student_id_keeps_direct_selection_behavior(self, mock_validate_exists, mock_new_doc):
		result = _resolve_student({"student": "STU-001"}, "PAR-001", "Trial Lesson")

		self.assertEqual(result, "STU-001")
		mock_validate_exists.assert_called_once_with("Student", "STU-001", "Student was not found.")
		mock_new_doc.assert_not_called()

	@patch("qas_custom.services.inquiry.frappe.new_doc")
	def test_school_visit_incomplete_student_identity_never_creates_student(self, mock_new_doc):
		for payload in (
			{"student_name": "Leo"},
			{"date_of_birth": "2018-08-17"},
			{},
		):
			with self.subTest(payload=payload):
				self.assertIsNone(_resolve_student(payload, "PAR-001", "School Visit"))
		mock_new_doc.assert_not_called()

	@patch("qas_custom.services.inquiry.frappe.new_doc")
	@patch(
		"qas_custom.services.inquiry.frappe.get_all",
		return_value=[{"name": "STU-001", "student_name": "  lEO   wong "}],
	)
	def test_school_visit_complete_student_identity_matches_by_parent_dob_and_normalized_name(
		self, mock_get_all, mock_new_doc
	):
		result = _resolve_student(
			{"student_name": "Leo Wong", "date_of_birth": "2018-08-17"},
			"PAR-001",
			"School Visit",
		)

		self.assertEqual(result, "STU-001")
		mock_get_all.assert_called_once_with(
			"Student",
			filters={"guardian": "PAR-001", "date_of_birth": "2018-08-17"},
			fields=["name", "student_name"],
			limit_page_length=0,
		)
		mock_new_doc.assert_not_called()

	@patch("qas_custom.services.inquiry.frappe.get_all", return_value=[{"name": "STU-OLDER", "student_name": "Ava Wong"}])
	@patch("qas_custom.services.inquiry.frappe.new_doc")
	def test_same_parent_and_dob_with_different_name_creates_twin_student(self, mock_new_doc, _mock_get_all):
		student_doc = MagicMock()
		student_doc.name = "STU-YOUNGER"
		mock_new_doc.return_value = student_doc

		result = _resolve_student(
			{"student_name": "Mia Wong", "date_of_birth": "2018-08-17"},
			"PAR-001",
			"Trial Lesson",
		)

		self.assertEqual(result, "STU-YOUNGER")
		self.assertEqual(student_doc.student_name, "Mia Wong")
		self.assertEqual(student_doc.guardian, "PAR-001")
		self.assertEqual(student_doc.date_of_birth, "2018-08-17")
		student_doc.insert.assert_called_once_with()

	def test_referral_claim_requires_referring_family_name_for_new_trials(self):
		self.assertTrue(is_referral_claim({"referral_source": "Google", "referral_detail": "Lindsey's mum"}))
		self.assertFalse(is_referral_claim({"referral_source": "Referral", "referral_detail": ""}))

	def test_referral_claim_preserves_formally_reviewed_historical_records(self):
		self.assertTrue(is_referral_claim({"referral_detail": "", "referral_status": "Verified"}))

	def test_normalizes_special_needs_for_webhook_and_clear_requests(self):
		from_alias = _normalize_inquiry_payload({"special_need": "  NDIS support  "})
		self.assertEqual(from_alias["special_needs"], "NDIS support")
		self.assertTrue(from_alias["_special_needs_provided"])

		clear_request = _normalize_inquiry_payload({"special_needs": ""})
		self.assertEqual(clear_request["special_needs"], "")
		self.assertTrue(clear_request["_special_needs_provided"])

		unrelated_inquiry = _normalize_inquiry_payload({"student_name": "Ava"})
		self.assertFalse(unrelated_inquiry["_special_needs_provided"])

	def test_formats_form_values_and_hides_technical_data(self):
		rows = _submission_data_rows(
			{
				"parent_name": {"first_name": "Kylie", "last_name": "Keioskie"},
				"student_name": {"first_name": "Mya"},
				"student_dob": "05/23/2013",
				"preferred_days": ["Monday", "Wednesday"],
				"has_medical_note": False,
				"webhook_token": "must-not-be-returned",
				"nested": {"csrf_token": "also-hidden", "visible_answer": "Shown"},
				"_submission": {"browser": "Chrome"},
			}
		)

		self.assertEqual(
			rows,
			[
				{"label": "parent name · first name", "value": "Kylie"},
				{"label": "parent name · last name", "value": "Keioskie"},
				{"label": "student name · first name", "value": "Mya"},
				{"label": "student dob", "value": "05/23/2013"},
				{"label": "preferred days", "value": "Monday, Wednesday"},
				{"label": "has medical note", "value": "No"},
				{"label": "nested · visible answer", "value": "Shown"},
			],
		)

	def test_returns_no_rows_for_missing_or_invalid_payload(self):
		self.assertEqual(_submission_data_rows(None), [])
		self.assertEqual(_submission_data_rows("not-json"), [])

	def test_normalizes_school_visit_fluent_fields_without_session(self):
		payload = _normalize_school_visit_webhook_payload(
			{
				"form_id": "12",
				"submission_id": "456",
				"parent_name": {"first_name": "Mia", "last_name": "Wong"},
				"student_name": {"first_name": "Leo"},
				"student_dob": "08/17/2018",
				"campus_to_visit": "Indooroopilly",
				"date_for_the_visit": "08/22/2026",
				"time_to_arrive": "10:30",
				"class_that_interest_you": "Creative Art - Beginner",
				"inquiry_type": "Trial Lesson",
				"course_session": "Should never be linked",
				"submitted_class_session": "Also must be ignored",
			}
		)

		self.assertEqual(payload["inquiry_type"], "School Visit")
		self.assertEqual(payload["source"], "Fluent Form")
		self.assertTrue(payload["create_parent"])
		self.assertEqual(payload["parent_name"], "Mia Wong")
		self.assertEqual(payload["student_name"], "Leo")
		self.assertEqual(str(payload["date_of_birth"]), "2018-08-17")
		self.assertEqual(payload["campus"], "Indooroopilly")
		self.assertEqual(payload["appointment_date"], "08/22/2026")
		self.assertEqual(payload["appointment_time"], "10:30")
		self.assertEqual(payload["preferred_course"], "Creative Art - Beginner")
		self.assertEqual(payload["course_session"], None)
		self.assertEqual(payload["submitted_class_session"], None)
		self.assertEqual(payload["external_submission_id"], "fluent_form:12:456")

	@patch("qas_custom.services.inquiry._build_webhook_response")
	@patch("qas_custom.services.inquiry.create_inquiry_core")
	@patch("qas_custom.services.inquiry._get_existing_webhook_inquiry", return_value=None)
	@patch("qas_custom.services.inquiry._validate_webhook_token")
	def test_school_visit_webhook_creates_from_dedicated_adapter(
		self, _mock_token, _mock_existing, mock_create, mock_response
	):
		mock_create.return_value = {"inquiry": {"id": "INQ-0001"}}
		mock_response.return_value = {"status": "created", "inquiry": "INQ-0001"}
		original = {
			"form_id": "12",
			"submission_id": "456",
			"parent_name": "Mia Wong",
			"student_name": "Leo",
			"campus_to_visit": "Indooroopilly",
			"date_for_the_visit": "08/22/2026",
			"time_to_arrive": "10:30",
			"inquiry_type": "Trial Lesson",
			"course_session": "Must not be used",
		}

		result = create_school_visit_webhook_data(original)

		self.assertEqual(result["status"], "created")
		payload = mock_create.call_args.args[0]
		self.assertEqual(payload["inquiry_type"], "School Visit")
		self.assertEqual(payload["course_session"], None)
		self.assertEqual(payload["submitted_student_name"], "Leo")
		self.assertIsNone(payload.get("submitted_student_dob"))
		self.assertEqual(payload["raw_webhook_payload"], original)
		self.assertEqual(mock_create.call_args.kwargs["source"], "Fluent Form")

	@patch("qas_custom.services.inquiry._build_webhook_response", return_value={"status": "duplicate"})
	@patch("qas_custom.services.inquiry.create_inquiry_core")
	@patch("qas_custom.services.inquiry._get_existing_webhook_inquiry", return_value="INQ-0001")
	@patch("qas_custom.services.inquiry._validate_webhook_token")
	def test_school_visit_webhook_reuses_duplicate_submission(
		self, _mock_token, _mock_existing, mock_create, _mock_response
	):
		result = create_school_visit_webhook_data({"form_id": "12", "submission_id": "456"})

		self.assertEqual(result["status"], "duplicate")
		mock_create.assert_not_called()
