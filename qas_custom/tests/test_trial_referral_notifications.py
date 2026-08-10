from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.trial_referral_notifications import _referral_message


class TestTrialReferralNotifications(TestCase):
	def referral_context(self, event_kind):
		return {
			"event_kind": event_kind,
			"school_name": "Queensland Art School",
			"amount": 30,
			"referred_parent_name": "Mia Chen",
			"referred_student_name": "Ava Chen",
			"portal_url": "https://portal.queenslandartschool.com/credit",
		}

	def test_trial_discount_email_names_the_referred_parent_and_student(self):
		with patch(
			"qas_custom.modules.notifications.trial_referral_notifications.frappe.format_value",
			return_value="$30.00",
		), patch(
			"qas_custom.modules.notifications.trial_referral_notifications._",
			side_effect=lambda message: message,
		):
			message = _referral_message(self.referral_context("trial_discount"))

		self.assertIn("Mia Chen", message)
		self.assertIn("Ava Chen", message)
		self.assertIn("discount on their trial class", message)

	def test_conversion_reward_email_names_the_referred_parent_and_student(self):
		with patch(
			"qas_custom.modules.notifications.trial_referral_notifications.frappe.format_value",
			return_value="$30.00",
		), patch(
			"qas_custom.modules.notifications.trial_referral_notifications._",
			side_effect=lambda message: message,
		):
			message = _referral_message(self.referral_context("conversion_reward"))

		self.assertIn("Mia Chen", message)
		self.assertIn("Ava Chen", message)
		self.assertIn("has now enrolled", message)
