from unittest import TestCase

from qas_custom.services.portal_invites import _term_parent_invite_source


class TestPortalInviteSources(TestCase):
	def test_term_bulk_invites_use_a_valid_log_source(self):
		self.assertEqual(
			"Bulk Never Invited",
			_term_parent_invite_source("Term 3 2026", "never_invited"),
		)

	def test_each_bulk_mode_uses_the_same_valid_log_source(self):
		self.assertEqual(
			"Bulk Never Invited",
			_term_parent_invite_source("Term 3 2026", "invited_not_logged_in"),
		)
