from unittest import TestCase

from qas_custom.modules.trial_referrals import is_referral_claim
from qas_custom.services.inquiry import _normalize_inquiry_payload, _submission_data_rows


class TestInquirySubmissionData(TestCase):
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
