"""PSU Go reset links retain their route without changing Parent identity checks."""
from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.api import parent_portal
from qas_custom.services import password_reset


class TestParentPasswordResetContext(TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(in_test=False)

    def test_psugo_request_uses_parent_identity_and_link_context(self):
        with patch.object(password_reset, "_request_password_reset", return_value={"ok": True}) as request:
            self.assertEqual(password_reset.request_password_reset("p@example.com", portal="psugo"), {"ok": True})
        request.assert_called_once_with("p@example.com", portal="parent", return_portal="psugo")
        with patch.object(password_reset, "_get_portal_base_url", return_value="https://portal.example.com"):
            self.assertEqual(password_reset._build_password_reset_link("token", portal="parent", return_portal="psugo"),
                             "https://portal.example.com/reset-password?token=token&portal=psugo")
            self.assertEqual(password_reset._build_password_reset_link("token", portal="parent"),
                             "https://portal.example.com/reset-password?token=token")
            with self.assertRaises(frappe.PermissionError):
                password_reset._build_password_reset_link("token", portal="teacher", return_portal="psugo")

    def test_validate_and_confirm_psugo_stay_parent_scoped(self):
        with patch.object(password_reset, "_validate_password_reset_token", return_value={"valid": True}) as validate:
            password_reset.validate_password_reset_token("token", portal="psugo")
        validate.assert_called_once_with("token", portal="parent")
        with patch.object(password_reset, "_confirm_password_reset", return_value={"ok": True}) as confirm:
            password_reset.confirm_password_reset("token", "password123", portal="psugo")
        confirm.assert_called_once_with("token", "password123", portal="parent")

    def test_parent_api_passes_psugo_and_rejects_other_portals(self):
        with patch.object(parent_portal, "request_password_reset", return_value={"ok": True}) as request:
            parent_portal.parent_portal_request_password_reset("p@example.com", portal="psugo")
        request.assert_called_once_with("p@example.com", portal="psugo")
        with patch.object(parent_portal, "validate_password_reset_token", return_value={"valid": True}) as validate:
            parent_portal.parent_portal_validate_password_reset_token("token", portal="psugo")
        validate.assert_called_once_with("token", portal="psugo")
        with patch.object(parent_portal, "confirm_password_reset", return_value={"ok": True}) as confirm:
            parent_portal.parent_portal_confirm_password_reset("token", "password123", portal="psugo")
        confirm.assert_called_once_with("token", "password123", portal="psugo")
        for portal in ("teacher", "campus_admin", "arbitrary"):
            with self.assertRaises(frappe.PermissionError):
                password_reset.request_password_reset("p@example.com", portal=portal)
            with self.assertRaises(frappe.PermissionError):
                password_reset.validate_password_reset_token("token", portal=portal)
            with self.assertRaises(frappe.PermissionError):
                password_reset.confirm_password_reset("token", "password123", portal=portal)
