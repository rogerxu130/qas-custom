from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.portal_invites import _parent_invite_log_history
from qas_custom.services.school_admin import PARENT_UPDATE_FIELDS, _is_parent_email_identity_conflict


class TestParentEmailCorrection(TestCase):
	def test_generic_parent_update_does_not_accept_identity_email_fields(self):
		self.assertNotIn("email", PARENT_UPDATE_FIELDS)
		self.assertNotIn("email_id", PARENT_UPDATE_FIELDS)
		self.assertNotIn("contact_email", PARENT_UPDATE_FIELDS)

	def test_current_family_customer_and_contact_are_not_email_conflicts(self):
		parent = type("Parent", (), {"name": "PARENT-0001", "get": lambda self, key: "CUST-0001" if key == "customer" else None})()
		self.assertFalse(_is_parent_email_identity_conflict("Customer", "CUST-0001", parent, {"CONTACT-0001"}))
		self.assertFalse(_is_parent_email_identity_conflict("Contact", "CONTACT-0001", parent, {"CONTACT-0001"}))
		self.assertTrue(_is_parent_email_identity_conflict("Customer", "CUST-OTHER", parent, {"CONTACT-0001"}))

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
