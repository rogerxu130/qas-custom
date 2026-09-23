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
        payload = [{"name": "C-OLD", "status": "Transferred", "bookable": False},
                   {"name": "C-EXPIRED", "status": "Expired", "bookable": False}]
        with patch.object(payg_portal.booking, "family_cards", return_value={"students": [], "cards": cards}), \
             patch.object(payg_portal.payg_read_models, "enrich_cards", return_value=payload) as enrich:
            result = payg_portal.payg_family_cards()
        enrich.assert_called_once_with(cards)
        self.assertEqual([item["bookable"] for item in result["cards"]], [False, False])
        self.assertEqual(result["cards"][1]["status"], "Expired")

    def test_booking_history_filters_by_server_resolved_family_and_student(self):
        rows = [frappe._dict(name="B-1", student="S-1", card="C-1", course_session="CS-1",
                             attendance_entry="ATT-1", status="Cancelled", cancellable_until=None,
                             cancelled_at="2026-09-01")]
        with patch.object(booking, "_family", return_value=frappe._dict(name="P-1")), \
             patch.object(booking, "_student") as student_guard, \
             patch.object(booking.frappe, "get_all", return_value=rows) as get_all, \
             patch.object(payg_portal.payg_read_models, "enrich_booking_history", return_value=[{"status": "Cancelled"}]):
            result = payg_portal.payg_booking_history(student="S-1")
        student_guard.assert_called_once_with("S-1", "P-1")
        self.assertEqual(get_all.call_args.kwargs["filters"], {"family_parent": "P-1", "student": "S-1"})
        self.assertEqual(result["items"][0]["status"], "Cancelled")

    def test_available_sessions_keeps_service_pagination_and_enriches_items(self):
        raw = {"items": [{"name": "CS-1"}], "has_more": True,
               "next_cursor": ("2026-09-30", "CS-1")}
        enriched = {**raw, "items": [{"name": "CS-1", "course": "C-1", "start_time": "09:00:00"}]}
        with patch.object(payg_portal.booking, "available_sessions", return_value=raw) as service, \
             patch.object(payg_portal.payg_read_models, "enrich_available_sessions", return_value=enriched) as enrich:
            result = payg_portal.payg_available_sessions("S-1", "C-1", cursor='["2026-09-24","CS-0"]', limit="10")
        service.assert_called_once_with("S-1", "C-1", cursor=["2026-09-24", "CS-0"], limit="10")
        enrich.assert_called_once_with(raw)
        self.assertEqual(result["next_cursor"], raw["next_cursor"])

    def test_admin_context_filters_family_and_support_view_read(self):
        frappe.local.db = SimpleNamespace(exists=Mock(return_value=True))
        products = [{"name": "PROD-1"}]
        rows = {"Student": [frappe._dict(name="S-1")],
                "QAS PAYG Card": [frappe._dict(name="CARD-1")],
                "QAS PAYG Booking": [frappe._dict(name="B-1")]}
        def get_all(doctype, **_kwargs):
            return rows[doctype]
        with patch.object(school_admin_payg, "get_support_view_token", return_value=""), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["School Admin"]), \
             patch.object(school_admin_payg.payg_read_models, "product_payloads", return_value=products), \
             patch.object(school_admin_payg.payg_read_models, "enrich_cards", return_value=[{"name": "CARD-1"}]), \
             patch.object(school_admin_payg.payg_read_models, "enrich_booking_history", return_value=[{"name": "B-1"}]), \
             patch.object(school_admin_payg.frappe, "get_all", side_effect=get_all) as query:
            self.assertEqual(school_admin_payg.payg_admin_context(), {"products": products})
            result = school_admin_payg.payg_admin_context("P-1")
        self.assertEqual((result["students"][0].name, result["cards"][0]["name"], result["bookings"][0]["name"]),
                         ("S-1", "CARD-1", "B-1"))
        self.assertTrue(all(call.kwargs["filters"] in ({"guardian": "P-1"}, {"family_parent": "P-1"})
                            for call in query.call_args_list))
        with patch.object(school_admin_payg, "get_support_view_token", return_value="token"), \
             patch.object(school_admin_payg, "get_support_view_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["School Admin"]), \
             self.assertRaises(frappe.PermissionError):
            school_admin_payg.payg_admin_context("P-OTHER")
        with patch.object(school_admin_payg, "get_support_view_token", return_value="token"), \
             patch.object(school_admin_payg, "get_support_view_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["School Admin"]), \
             patch.object(school_admin_payg.payg_read_models, "product_payloads", return_value=products), \
             patch.object(school_admin_payg.payg_read_models, "enrich_cards", return_value=[]), \
             patch.object(school_admin_payg.payg_read_models, "enrich_booking_history", return_value=[]), \
             patch.object(school_admin_payg.frappe, "get_all", return_value=[]):
            self.assertEqual(school_admin_payg.payg_admin_context("P-1")["family_parent"], "P-1")
        with patch.object(school_admin_payg, "get_support_view_token", return_value=""), \
             patch.object(school_admin_payg.frappe, "get_roles", return_value=["Campus Admin"]), \
             self.assertRaises(frappe.PermissionError):
            school_admin_payg.payg_admin_context()

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

    def test_invalid_cursor_and_limits_return_frappe_errors(self):
        with patch.object(payg_portal, "frappe") as fake:
            fake.throw.side_effect = ValueError
            with self.assertRaises(ValueError):
                payg_portal.payg_available_sessions("S", "C", cursor="not-json")
            fake.throw.assert_called()
        with patch.object(booking, "_family", return_value=frappe._dict(name="P-1")), \
             patch.object(booking.frappe, "throw", side_effect=ValueError) as error:
            with self.assertRaises(ValueError):
                booking.family_booking_history(limit="bogus")
            error.assert_called()
        with patch.object(booking, "_family", return_value=frappe._dict(name="P-1")), \
             patch.object(booking, "_student", return_value=frappe._dict(name="S-1")), \
             patch.object(booking.frappe, "get_doc", return_value=frappe._dict(name="C-1")), \
             patch.object(booking.frappe, "throw", side_effect=ValueError) as error:
            with self.assertRaises(ValueError):
                booking.available_sessions("S-1", "C-1", cursor=[])
            error.assert_called()
