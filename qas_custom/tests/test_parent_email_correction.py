from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.portal_invites import _parent_invite_log_history
from qas_custom.services.school_admin import PARENT_UPDATE_FIELDS


class TestParentEmailCorrection(TestCase):
	def test_generic_parent_update_does_not_accept_identity_email_fields(self):
		self.assertNotIn("email", PARENT_UPDATE_FIELDS)
		self.assertNotIn("email_id", PARENT_UPDATE_FIELDS)
		self.assertNotIn("contact_email", PARENT_UPDATE_FIELDS)

	@patch("qas_custom.services.portal_invites.frappe")
	def test_invite_history_only_counts_current_user_and_email(self, frappe_mock):
		frappe_mock.db.exists.return_value = True
		frappe_mock.db.count.return_value = 0

		result = _parent_invite_log_history(
			"PARENT-0001",
			linked_user="new.parent@example.com",
			email="NEW.PARENT@EXAMPLE.COM",
		)

		self.assertEqual({"invited": False, "invite_count": 0}, result)
		frappe_mock.db.count.assert_called_once_with(
			"Parent Portal Invite Log",
			{
				"parent": "PARENT-0001",
				"status": "Sent",
				"user": "new.parent@example.com",
				"email": "new.parent@example.com",
			},
		)
