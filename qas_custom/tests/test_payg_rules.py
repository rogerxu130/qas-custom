"""Pure PAYG date and card-selection contract tests."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase

from qas_custom.modules.payg.rules import (
    BRISBANE, PreviewStale, add_six_months, as_brisbane_datetime,
    can_book, can_parent_cancel, choose_card, confirm_preview_card, expiry_end,
    card_covers_session,
)
from qas_custom.modules.payg.ledger import apply_entries


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=BRISBANE)


def card(name, issued="2026-09-23", expiry="2027-03-23", available=1, status="Active"):
    return SimpleNamespace(name=name, issued_on=issued, expires_on=expiry,
                           available_count=available, status=status)


class TestPaygRules(TestCase):
    def test_six_calendar_months_clamp_leap_year_and_cross_year(self):
        self.assertEqual(add_six_months(date(2026, 8, 31)), date(2027, 2, 28))
        self.assertEqual(add_six_months(date(2023, 8, 31)), date(2024, 2, 29))
        self.assertEqual(add_six_months(date(2026, 10, 31)), date(2027, 4, 30))

    def test_frappe_datetime_adapter_and_local_expiry_end(self):
        self.assertEqual(as_brisbane_datetime("2026-09-23 09:00:00"), local("2026-09-23T09:00:00"))
        self.assertEqual(as_brisbane_datetime(datetime(2026, 9, 22, 23, tzinfo=timezone.utc)),
                         local("2026-09-23T09:00:00"))
        self.assertEqual(expiry_end("2027-03-23"), local("2027-03-23T23:59:59.999999"))

    def test_issue_day_and_expiry_day_inclusive(self):
        issued = card("A", issued="2026-09-23", expiry="2026-10-01")
        self.assertTrue(card_covers_session(issued, now=local("2026-09-23T00:00:00"),
                                            session_start=local("2026-10-01T23:59:59")))
        self.assertFalse(card_covers_session(issued, now=local("2026-10-02T00:00:00"),
                                             session_start=local("2026-10-02T00:00:00")))
        self.assertFalse(card_covers_session(issued, now=local("2026-09-22T23:59:59"),
                                             session_start=local("2026-09-23T10:00:00")))

    def test_exact_72_hours_bookable_but_parent_cancel_strictly_earlier(self):
        start = local("2026-10-01T10:00:00")
        boundary = start - timedelta(hours=72)
        self.assertTrue(can_book(now=boundary, session_start=start))
        self.assertFalse(can_book(now=boundary + timedelta(microseconds=1), session_start=start))
        self.assertFalse(can_parent_cancel(now=boundary, session_start=start, booking_status="Locked"))
        self.assertTrue(can_parent_cancel(now=boundary - timedelta(microseconds=1), session_start=start,
                                          booking_status="Locked"))

    def test_selection_skips_expired_unavailable_paused_and_transferred(self):
        cards = [card("A", expiry="2026-10-01"), card("B", expiry="2026-12-01"),
                 card("C", expiry="2026-11-01", available=0),
                 card("D", expiry="2026-11-01", status="Paused"),
                 card("E", expiry="2026-11-01", status="Transferred")]
        chosen = choose_card(cards, now=local("2026-09-23T09:00:00"),
                             session_start=local("2026-11-01T10:00:00"))
        self.assertEqual(chosen.name, "B")

    def test_selection_order_and_preview_staleness(self):
        cards = [card("Z", issued="2026-08-02"), card("B", issued="2026-08-01"),
                 card("A", issued="2026-08-01")]
        now, start = local("2026-09-23T09:00:00"), local("2026-10-01T10:00:00")
        self.assertEqual(choose_card(cards, now=now, session_start=start).name, "A")
        self.assertEqual(confirm_preview_card(cards, now=now, session_start=start,
                                              preview_card="A").name, "A")
        with self.assertRaisesRegex(PreviewStale, "refresh"):
            confirm_preview_card(cards, now=now, session_start=start, preview_card="B")
        with self.assertRaisesRegex(PreviewStale, "refresh"):
            confirm_preview_card([], now=now, session_start=start, preview_card="A")
        with self.assertRaisesRegex(PreviewStale, "refresh"):
            confirm_preview_card(cards, now=now, session_start=start, preview_card="")
        with self.assertRaises(TypeError):
            confirm_preview_card(cards, now=now, session_start=start)

    def test_confirmation_rejects_selection_change_after_preview(self):
        cards = [card("A", expiry="2026-10-01"), card("B", expiry="2026-12-01")]
        now, start = local("2026-09-23T09:00:00"), local("2026-09-30T10:00:00")
        preview_name = choose_card(cards, now=now, session_start=start).name
        self.assertEqual(preview_name, "A")
        cards[0].available_count = 0
        self.assertEqual(choose_card(cards, now=now, session_start=start).name, "B")
        with self.assertRaisesRegex(PreviewStale, "refresh"):
            confirm_preview_card(cards, now=now, session_start=start, preview_card=preview_name)

    def test_transferred_source_stays_unselectable_after_late_return(self):
        source = card("SOURCE", status="Transferred", available=1)
        self.assertIsNone(choose_card([source], now=local("2026-09-23T09:00:00"),
                                      session_start=local("2026-10-01T10:00:00")))

    def test_expiry_and_date_only_renewal_preserve_ledger(self):
        rows = (SimpleNamespace(kind="Issue", available_delta=10, reserved_delta=0,
                                consumed_delta=0, operation_key="issue"),)
        original = card("A", issued="2026-01-31", expiry="2026-07-31", available=10)
        before = apply_entries(rows)
        self.assertIsNone(choose_card([original], now=local("2026-08-01T00:00:00"),
                                      session_start=local("2026-08-04T10:00:00")))
        self.assertEqual(apply_entries(rows), before)
        renewed = card("A", issued=original.issued_on,
                       expiry=add_six_months(original.expires_on), available=original.available_count)
        self.assertEqual(renewed.expires_on, date(2027, 1, 31))
        self.assertEqual(choose_card([renewed], now=local("2026-08-01T00:00:00"),
                                     session_start=local("2026-08-04T10:00:00")).name, "A")
        self.assertEqual(rows, (rows[0],))
        self.assertEqual(apply_entries(rows), before)
        self.assertEqual(tuple(before), (10, 0, 0))
