from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.trial_referrals import prepare_referral_review


class ReferralDocument(dict):
	def __init__(self, **values):
		super().__init__(values)
		self.meta = SimpleNamespace(has_field=lambda fieldname: fieldname in {
			"referral_status",
			"referral_resume_status",
			"review_reason",
		})

	def set(self, fieldname, value):
		self[fieldname] = value


class TestTrialReferralReviewReason(TestCase):
	@patch("qas_custom.modules.trial_referrals._", side_effect=lambda value: value)
	def test_pending_referral_stores_an_explicit_review_reason(self, _mock_translate):
		doc = ReferralDocument(referral_detail="An existing QAS family")

		self.assertTrue(prepare_referral_review(doc, resume_status="Booked"))

		self.assertEqual(doc["referral_status"], "Pending Verification")
		self.assertEqual(doc["referral_resume_status"], "Booked")
		self.assertEqual(
			doc["review_reason"],
			"Referral details require verification before the Trial Invoice can be issued.",
		)

	@patch("qas_custom.modules.trial_referrals._", side_effect=lambda value: value)
	def test_referral_reason_is_appended_to_a_scheduling_reason_once(self, _mock_translate):
		doc = ReferralDocument(
			referral_detail="An existing QAS family",
			review_reason="Multiple Weekly Timeslots matched the submitted campus, weekday, and time.",
		)

		prepare_referral_review(doc)
		prepare_referral_review(doc)

		self.assertEqual(
			doc["review_reason"],
			"Multiple Weekly Timeslots matched the submitted campus, weekday, and time. "
			"Referral details require verification before the Trial Invoice can be issued.",
		)
