from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.campus_admin_accounts import (
	_get_campus_admin_invite_status,
	_normalise_campuses,
	_normalise_email,
	get_active_campus_admin_emails,
)
from qas_custom.services.password_reset import (
	PORTAL_CAMPUS_ADMIN,
	_build_password_reset_link,
	_is_portal_reset_user,
	request_campus_admin_password_reset,
)


class TestCampusAdminAccounts(TestCase):
	def test_normalises_email_and_unique_campuses(self):
		self.assertEqual(_normalise_email(" Admin@Example.COM "), "admin@example.com")
		self.assertEqual(_normalise_campuses('["Indooroopilly", "Indooroopilly", "Springfield"]'), ["Indooroopilly", "Springfield"])

	@patch("qas_custom.services.campus_admin_accounts._get_enabled_user_emails")
	@patch("qas_custom.services.campus_admin_accounts._get_active_profile_users")
	@patch("qas_custom.services.campus_admin_accounts._get_assigned_profile_names")
	@patch("qas_custom.services.campus_admin_accounts._campus_admin_profile_available", return_value=True)
	def test_active_campus_admin_emails_are_enabled_unique_and_normalised(
		self,
		_mock_available,
		mock_profile_names,
		mock_user_names,
		mock_emails,
	):
		mock_profile_names.return_value = ["CAP-001", "CAP-002"]
		mock_user_names.return_value = ["admin-one@example.com", "admin-two@example.com"]
		mock_emails.return_value = ["admin-one@example.com", "admin-two@example.com", "admin-two@example.com"]

		self.assertEqual(
			get_active_campus_admin_emails("Indooroopilly"),
			["admin-one@example.com", "admin-two@example.com"],
		)

	@patch("qas_custom.services.campus_admin_accounts._invite_history", return_value={"invited": False, "invite_count": 0})
	@patch("qas_custom.services.campus_admin_accounts._", side_effect=lambda value: value)
	def test_invite_status_is_never_invited_before_login(self, _mock_translate, _mock_history):
		status = _get_campus_admin_invite_status(
			SimpleNamespace(name="CAP-001"),
			{"last_login": None, "last_active": None},
		)

		self.assertEqual(status["status"], "never_invited")

	@patch("qas_custom.services.password_reset._active_campus_admin_profile_exists", return_value=True)
	@patch("qas_custom.services.password_reset._portal_user_enabled", return_value=True)
	def test_campus_admin_reset_requires_enabled_active_profile(self, _mock_enabled, _mock_profile):
		self.assertTrue(_is_portal_reset_user("admin@example.com", PORTAL_CAMPUS_ADMIN))

	@patch("qas_custom.services.password_reset._get_portal_base_url", return_value=None)
	@patch("qas_custom.services.password_reset.get_url", return_value="https://portal.example.com/reset-password")
	def test_campus_admin_invite_link_carries_portal_type(self, _mock_get_url, _mock_base_url):
		link = _build_password_reset_link("secret-token", portal=PORTAL_CAMPUS_ADMIN)

		self.assertEqual(
			link,
			"https://portal.example.com/reset-password?token=secret-token&portal=campus_admin",
		)

	@patch("qas_custom.services.password_reset._request_password_reset", return_value={"ok": True})
	def test_campus_admin_password_reset_uses_campus_admin_portal(self, mock_request):
		self.assertEqual(request_campus_admin_password_reset("admin@example.com"), {"ok": True})
		mock_request.assert_called_once_with("admin@example.com", portal=PORTAL_CAMPUS_ADMIN)
