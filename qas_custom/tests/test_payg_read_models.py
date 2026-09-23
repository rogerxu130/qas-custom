"""PAYG family/admin read payloads stay useful without extra per-row queries."""
from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import payg_read_models


class TestPaygReadModels(TestCase):
    def setUp(self):
        self.calls = []
        fixtures = {
            "Course Sessions": [frappe._dict(name="CS-1", session_date="2026-09-30",
                                               weekly_timeslot="W-1", teacher_override="T-2")],
            "Weekly Timeslot": [frappe._dict(name="W-1", course="C-1", term="TERM-1", campus="CAM-1",
                                              classroom="ROOM-1", teacher="T-1", start_time="09:00:00",
                                              end_time="10:00:00")],
            "Course": [frappe._dict(name="C-1", course_name="Drawing")],
            "Teacher": [frappe._dict(name="T-2", teacher_name="Ada")],
            "QAS PAYG Card": [frappe._dict(name="CARD-1", expires_on="2027-01-01", status="Transferred")],
            "QAS PAYG Product": [frappe._dict(name="PROD-1", course="C-1", sessions_per_card=10,
                                               standard_card_price=400, enabled=1)],
        }
        def get_all(doctype, **kwargs):
            self.calls.append((doctype, kwargs))
            return fixtures.get(doctype, [])
        self.get_all = Mock(side_effect=get_all)
        self.patches = [patch.object(payg_read_models.frappe, "get_all", self.get_all),
                        patch.object(payg_read_models, "get_datetime_in_timezone",
                                     return_value=datetime(2026, 9, 24, 9))]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def test_available_item_contains_schedule_and_keeps_cursor(self):
        result = payg_read_models.enrich_available_sessions(
            {"items": [{"name": "CS-1", "card": "CARD-1", "bookable": True}],
             "has_more": True, "next_cursor": ("2026-09-30", "CS-1")})
        item = result["items"][0]
        self.assertEqual((item["course_label"], item["start_time"], item["teacher_label"]),
                         ("Drawing", "09:00:00", "Ada"))
        self.assertEqual((item["term"], item["campus"], item["classroom"]),
                         ("TERM-1", "CAM-1", "ROOM-1"))
        self.assertEqual(result["next_cursor"], ("2026-09-30", "CS-1"))
        self.assertEqual(sum(dt == "Course Sessions" for dt, _ in self.calls), 1)

    def test_history_cancel_window_is_strictly_more_than_72_hours(self):
        booking = frappe._dict(name="B-1", student="S-1", card="CARD-1", course_session="CS-1",
                               attendance_entry="ATT-1", status="Reserved", cancelled_at=None,
                               cancellable_until=None, card_expires_on_snapshot="2026-12-01")
        item = payg_read_models.enrich_booking_history([booking])[0]
        self.assertEqual(item["cancel_deadline"], "2026-09-27 09:00:00+10:00")
        self.assertTrue(item["can_cancel"])
        self.assertEqual((item["course"], item["course_label"], item["card_expires_on"]),
                         ("C-1", "Drawing", "2027-01-01"))
        self.assertEqual((item["card_expires_on_snapshot"], item["card_status"]),
                         ("2026-12-01", "Transferred"))
        with patch.object(payg_read_models, "get_datetime_in_timezone", return_value=datetime(2026, 9, 27, 9)):
            self.assertFalse(payg_read_models.enrich_booking_history([booking])[0]["can_cancel"])

    def test_card_labels_use_course_and_product_metadata(self):
        card = frappe._dict(name="CARD-1", course="C-1", product="PROD-1", issued_on="2026-07-01",
                            expires_on="2027-01-01", status="Active", available_count=4,
                            reserved_count=1, consumed_count=5)
        item = payg_read_models.enrich_cards([card])[0]
        self.assertEqual((item["course_label"], item["product_label"]), ("Drawing", "Drawing · 10 sessions"))
        self.assertEqual((item["available_count"], item["reserved_count"], item["consumed_count"]), (4, 1, 5))

    def test_product_options_include_price_and_labels(self):
        products = payg_read_models.product_payloads()
        self.assertEqual(products[0]["standard_card_price"], 400)
        self.assertEqual((products[0]["course_label"], products[0]["product_label"]),
                         ("Drawing", "Drawing · 10 sessions"))
