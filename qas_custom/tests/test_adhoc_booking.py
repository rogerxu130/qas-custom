"""Adhoc confirmation keeps its payment and cancellation behavior at the shared boundary."""

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import adhoc_booking as subject


class TestAdhocBooking(TestCase):
    def test_discovery_keeps_legacy_60_day_and_100_row_window(self):
        timeslot = frappe._dict(name="W-1", course="Art", campus="Campus", start_time="10:00")
        get_all = Mock(side_effect=[[timeslot], []])
        fake = SimpleNamespace(get_all=get_all)
        with patch.object(subject, "frappe", fake), \
             patch.object(subject, "require_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(subject, "get_adhoc_students_for_parent", return_value=[frappe._dict(name="S-1")]), \
             patch.object(subject, "validate_student_filter", return_value="S-1"), \
             patch.object(subject, "today", return_value="2026-09-24"):
            self.assertEqual(subject.get_available_sessions_data(student="S-1", course="Art"), {"items": []})
        self.assertEqual([call.args[0] for call in get_all.call_args_list],
                         ["Weekly Timeslot", "Course Sessions"])
        session_query = get_all.call_args_list[1].kwargs
        self.assertEqual(session_query["filters"]["session_date"],
                         ["between", [subject.getdate("2026-09-24"), subject.getdate("2026-11-23")]])
        self.assertEqual(session_query["limit"], 100)

    def test_preview_keeps_trial_fee_and_customer_balance(self):
        parent = frappe._dict(name="P-1", customer="CUST-1")
        pupil = frappe._dict(name="S-1")
        context = {"session": frappe._dict(name="CS-1"),
                   "timeslot": frappe._dict(course="Art")}
        with patch.object(subject, "require_parent", return_value=parent), \
             patch.object(subject, "get_adhoc_students_for_parent", return_value=[pupil]), \
             patch.object(subject, "validate_student_filter", return_value="S-1"), \
             patch.object(subject, "validate_booking_context", return_value=context), \
             patch.object(subject, "get_trial_class_fee", return_value=25) as fee, \
             patch.object(subject, "get_customer_balance_summary", return_value={"available": 42}) as balance, \
             patch.object(subject, "build_student_summary", return_value={"id": "S-1"}), \
             patch.object(subject, "build_session_item", return_value={"id": "CS-1"}), \
             patch.object(subject, "get_adhoc_rules", return_value={}):
            result = subject.preview_booking_data("S-1", "CS-1")
        self.assertEqual((result["fee_amount"], result["balance"]), (25, {"available": 42}))
        fee.assert_called_once_with("Art")
        balance.assert_called_once_with("CUST-1")

    def test_confirm_uses_locked_reservation_after_credit_check(self):
        events = []
        parent = frappe._dict(name="P-1", customer="CUST-1")
        session = frappe._dict(name="CS-1", session_date="2026-10-03")
        slot = frappe._dict(course="Art", campus="Campus", start_time="10:00", end_time="11:00")
        booking = frappe._dict(name="AB-1", insert=Mock(side_effect=lambda **kw: events.append("insert")), save=Mock())
        db = SimpleNamespace(sql=Mock(side_effect=lambda *_args, **_kw: events.append("student_lock")), rollback=Mock())
        fake = SimpleNamespace(db=db, get_doc=Mock(return_value=booking), session=frappe._dict(user="parent@example.com"))
        context = {"session": session, "timeslot": slot, "session_start": datetime(2026, 10, 3, 10)}
        with patch.object(subject, "frappe", fake), \
             patch.object(subject, "reject_support_view_write"), \
             patch.object(subject, "require_parent", return_value=parent), \
             patch.object(subject, "get_adhoc_students_for_parent", return_value=[frappe._dict(name="STU-1")]), \
             patch.object(subject, "validate_student_filter", return_value="STU-1"), \
             patch.object(subject, "validate_booking_context", return_value=context), \
             patch.object(subject, "get_trial_class_fee", return_value=25), \
             patch.object(subject, "validate_booking_credit", side_effect=lambda _doc: events.append("credit")), \
             patch.object(subject, "reserve_regular_place", side_effect=lambda *_args, **_kwargs: events.append("reserve") or "ATT-1") as reserve, \
             patch.object(subject, "add_booking_history"), \
             patch.object(subject, "build_booking_item", return_value={"booking_id": "AB-1"}), \
             patch.object(subject, "get_adhoc_rules", return_value={}):
            result = subject.create_booking_data("STU-1", "CS-1", 1)
        self.assertEqual(events, ["student_lock", "insert", "credit", "reserve"])
        self.assertEqual(db.sql.call_args.args[1], ("STU-1",))
        payload = fake.get_doc.call_args.args[0]
        self.assertEqual(payload["fee_amount"], 25)
        self.assertEqual(payload["payment_status"], "No Charge Yet")
        self.assertEqual(reserve.call_args.args, ("STU-1", "CS-1", "Adhoc Booking", "AB-1", "Pay-as-you-go"))
        self.assertTrue(callable(reserve.call_args.kwargs["validate_business"]))
        self.assertEqual(result["booking"]["booking_id"], "AB-1")

    def test_locked_callback_enforces_notice_boundary(self):
        session = frappe._dict(session_date="2026-10-03")
        locked_slot = frappe._dict(start_time="10:00")
        callback = lambda: subject._validate_locked_adhoc_context(session, locked_slot)
        with patch.object(subject, "now_datetime", return_value=datetime(2026, 9, 30, 10)):
            callback()  # Exactly 72 hours remains valid.
        with patch.object(subject, "now_datetime", return_value=datetime(2026, 9, 30, 10, 0, 1)), \
             patch.object(subject.frappe, "throw", side_effect=ValueError):
            with self.assertRaises(ValueError):
                callback()

    def test_cancel_still_deletes_legacy_attendance(self):
        booking = frappe._dict(name="AB-1", parent="P-1", status="Reserved", cancellable_until=datetime(2026, 10, 2), payment_status="Awaiting", save=Mock())
        with patch.object(subject, "require_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(subject, "reject_support_view_write"), \
             patch.object(subject.frappe, "get_doc", return_value=booking), \
             patch.object(subject, "now_datetime", return_value=datetime(2026, 9, 23)), \
             patch.object(subject, "remove_adhoc_attendance_for_booking", return_value=True) as remove, \
             patch.object(subject, "add_booking_history"), \
             patch.object(subject, "build_booking_item", return_value={}):
            subject.cancel_booking_data("AB-1")
        remove.assert_called_once_with("AB-1")
        self.assertEqual(booking.status, "Cancelled")
        self.assertEqual(booking.payment_status, "No Charge Yet")
