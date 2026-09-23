"""Contract checks for the thin PAYG portal adapters."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.api import payg_portal, school_admin_payg
from qas_custom.modules.payg import booking


class TestPaygPortalAPI(TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(in_test=False)
        frappe.local.session = frappe._dict(user="test@example.com")

    def test_parent_commands_never_accept_a_family_override(self):
        with patch.object(payg_portal.booking, "family_cards", return_value={"students": [], "cards": []}) as cards:
            self.assertEqual(payg_portal.payg_family_cards(), {"students": [], "cards": []})
            cards.assert_called_once_with(student=None)
        with patch.object(payg_portal.booking, "confirm_booking", return_value=frappe._dict(name="B-1", status="Reserved", card="C-1")) as confirm:
            result = payg_portal.payg_confirm_booking("S-1", "CS-1", "C-1", "key-1", confirmed_rules=1)
            self.assertEqual(result["booking"], "B-1")
            confirm.assert_called_once_with("S-1", "CS-1", "C-1", "key-1", confirmed_rules=True)

    def test_transferred_and_expired_cards_remain_visible_but_unbookable(self):
        cards = [frappe._dict(name="C-OLD", course="ART", product="PROD", issued_on="2026-01-01",
                              expires_on="2027-01-01", status="Transferred", available_count=1,
                              reserved_count=0, consumed_count=9),
                 frappe._dict(name="C-EXPIRED", course="ART", product="PROD", issued_on="2025-01-01",
                              expires_on="2025-07-01", status="Active", available_count=2,
                              reserved_count=0, consumed_count=8)]
        with patch.object(payg_portal.booking, "family_cards", return_value={"students": [], "cards": cards}), \
             patch.object(payg_portal, "today", return_value="2026-09-24"):
            result = payg_portal.payg_family_cards()
        self.assertEqual([item["bookable"] for item in result["cards"]], [False, False])
        self.assertEqual(result["cards"][1]["status"], "Expired")

    def test_booking_history_filters_by_server_resolved_family_and_student(self):
        rows = [frappe._dict(name="B-1", student="S-1", card="C-1", course_session="CS-1",
                             attendance_entry="ATT-1", status="Cancelled", cancellable_until=None,
                             cancelled_at="2026-09-01")]
        with patch.object(booking, "_family", return_value=frappe._dict(name="P-1")), \
             patch.object(booking, "_student") as student_guard, \
             patch.object(booking.frappe, "get_all", return_value=rows) as get_all:
            result = payg_portal.payg_booking_history(student="S-1")
        student_guard.assert_called_once_with("S-1", "P-1")
        self.assertEqual(get_all.call_args.kwargs["filters"], {"family_parent": "P-1", "student": "S-1"})
        self.assertEqual(result["items"][0]["status"], "Cancelled")

    def test_admin_requires_school_admin_and_rejects_support_view(self):
        with patch.object(school_admin_payg, "get_support_view_token", return_value="token"), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["School Admin"]), \
             self.assertRaises(frappe.PermissionError):
            school_admin_payg.payg_create_purchase_operation("P", "PROD", "key")
        for role in ("Campus Admin", "Teacher", "Parent"):
            with patch.object(school_admin_payg, "get_support_view_token", return_value=""), \
                 patch.object(school_admin_payg.frappe, "get_roles", return_value=[role]), \
                 self.assertRaises(frappe.PermissionError):
                school_admin_payg.payg_create_purchase_operation("P", "PROD", "key")

    def test_exchange_returns_operation_and_negative_delta_hint(self):
        operation = frappe._dict(name="OP-1", source_card="C-1", target_card="C-2",
                                 quantity=3, price_delta=-30, invoice=None)
        with patch.object(school_admin_payg, "get_support_view_token", return_value=""), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["School Admin"]), \
             patch.object(school_admin_payg.payg_drafts, "exchange_card_with_draft", return_value={"operation": operation, "invoice": None, "reason": "non_positive_exchange_delta"}) as exchange:
            result = school_admin_payg.payg_exchange_card("C-1", "PROD", "exchange-key", "invoice-key")
        self.assertEqual((result["source_card"], result["target_card"], result["transferred_quantity"]),
                         ("C-1", "C-2", 3))
        self.assertTrue(result["manual_refund_review_required"])
        exchange.assert_called_once()
