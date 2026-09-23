"""PAYG cancellation and lock command contracts without a connected site."""
from datetime import datetime, date, timezone
import json
from unittest import TestCase
from unittest.mock import patch

import frappe
from qas_custom.modules.payg import booking, cancellation
from qas_custom.modules.attendance import commands as attendance_commands
from qas_custom.tasks import payg_booking_tasks
from qas_custom.services import teacher_portal
from qas_custom.tests import test_payg_booking as booking_fixture


class TestCancellation(TestCase):
    add = booking_fixture.TestBooking.add
    get_doc = booking_fixture.TestBooking.get_doc
    get_all = booking_fixture.TestBooking.get_all
    value = booking_fixture.TestBooking.value
    reserve = booking_fixture.TestBooking.reserve

    def setUp(self):
        booking_fixture.TestBooking.setUp(self)
        self.fake.get_roles = lambda _user: ["School Admin"]
        self.fake.session.user = "admin@example.com"
        self.fake.db.commit = self.fake.db.rollback.__class__()
        self.fake.db.set_value.side_effect = self.set_value
        self.add("QAS PAYG Booking", "PB-1", family_parent="P-1", student="S-1", card="A",
                 course_session="CS-1", status="Reserved", attendance_entry="ATT-1",
                 cancellable_until=datetime(2026, 9, 24, 10), flags=frappe._dict())
        self.add("QAS PAYG Entry", "RESERVE-1", card="A", booking="PB-1", kind="Reserve",
                 operation_key="reserve:PB-1", available_delta=-1, reserved_delta=1, consumed_delta=0)
        self.card.available_count = 9
        self.card.reserved_count = 1
        self.attendance = self.add("Class Attendance Entry", "ATT-1", student="S-1",
                                   course_session="CS-1", status="Absent",
                                   source_doctype="QAS PAYG Booking", source_document="PB-1",
                                   marked_by="teacher@example.com", marked_at=datetime(2026, 9, 27, 11))
        booking.session_resources.active_rows.return_value = [frappe._dict(name="ATT-1")]
        self.extra = [patch.object(cancellation, "frappe", self.fake),
                      patch.object(cancellation, "_now", return_value=datetime(2026, 9, 27, 12)),
                      patch.object(cancellation.session_resources, "active_rows", return_value=[frappe._dict(name="ATT-1")]),
                      patch.object(payg_booking_tasks, "frappe", self.fake),
                      patch.object(payg_booking_tasks, "_now", return_value=datetime(2026, 9, 24, 10))]
        for item in self.extra:
            item.start(); self.addCleanup(item.stop)
        original = self.value
        def value(dt, key, field, **kwargs):
            if dt == "QAS PAYG Entry" and isinstance(key, dict):
                entry = next((doc for (kind, _), doc in self.state["docs"].items()
                              if kind == dt and doc.get("operation_key") == key.get("operation_key")), None)
                return entry.get(field) if entry else None
            return original(dt, key, field, **kwargs)
        self.db.get_value.side_effect = value
        self.db.exists.side_effect = lambda dt, filters: any(
            kind == dt and all(doc.get(key) == expected for key, expected in filters.items())
            for (kind, _), doc in self.state["docs"].items())

    def set_value(self, dt, name, field, value=None, **kwargs):
        if isinstance(field, dict):
            self.state["docs"][(dt, name)].update(field)
        else:
            self.state["docs"][(dt, name)][field] = value

    def sql(self, query, params, **kwargs):
        if "FROM `tabQAS PAYG Entry`" in query:
            entries = [doc for (dt, _), doc in self.state["docs"].items()
                       if dt == "QAS PAYG Entry"]
            if "operation_key=%s" in query:
                return [frappe._dict(reason=doc.get("reason")) for doc in entries
                        if doc.operation_key == params[0]]
            return [frappe._dict(name=doc.name) for doc in entries
                    if doc.booking == params[0] and doc.kind == "Consume"]
        return booking_fixture.TestBooking.sql(self, query, params, **kwargs)

    def entries(self, kind):
        return [doc for (dt, _), doc in self.state["docs"].items() if dt == "QAS PAYG Entry" and doc.kind == kind]

    def test_admin_cancels_completed_booking_once_after_72h_and_preserves_attendance(self):
        booking_doc = self.state["docs"][("QAS PAYG Booking", "PB-1")]
        booking_doc.status = "Completed"
        self.add("QAS PAYG Entry", "CONSUME-1", card="A", booking="PB-1", kind="Consume",
                 operation_key="consume:PB-1", available_delta=0, reserved_delta=-1, consumed_delta=1)
        self.card.reserved_count = 0
        self.card.consumed_count = 1
        first = cancellation.cancel_by_admin("PB-1", reason="School closure", request_key="cancel-1")
        second = cancellation.cancel_by_admin("PB-1", reason="School closure", request_key="cancel-1")
        self.assertIs(first, second)
        self.assertEqual(first.card, "A")
        self.assertEqual(len(self.entries("Return")), 1)
        self.assertEqual(json.loads(self.entries("Return")[0].reason),
                         {"admin_request_key": "cancel-1", "reason": "School closure"})
        self.assertEqual((self.card.available_count, self.card.consumed_count), (10, 0))
        self.assertEqual((self.attendance.status, self.attendance.marked_by), ("Cancelled", "teacher@example.com"))
        self.assertEqual(self.attendance.source_document, "PB-1")
        self.assertIn("Student:S-1", self.events)
        self.assertLess(self.events.index("Student:S-1"), self.events.index("QAS PAYG Booking:PB-1"))
        with self.assertRaisesRegex(ValueError, "another.*key"):
            cancellation.cancel_by_admin("PB-1", reason="School closure", request_key="cancel-2")

    def test_source_card_status_and_expiry_are_unchanged(self):
        self.card.status = "Transferred"
        self.card.expires_on = date(2026, 9, 20)
        cancellation.cancel_by_admin("PB-1", reason="Correction", request_key="return-old")
        self.assertEqual((self.card.status, self.card.expires_on), ("Transferred", date(2026, 9, 20)))
        self.assertEqual(len(self.entries("Return")), 1)

    def test_admin_role_reason_key_and_support_view_required(self):
        for reason, key in (("", "k"), ("reason", "")):
            with self.assertRaises(ValueError):
                cancellation.cancel_by_admin("PB-1", reason=reason, request_key=key)
        self.fake.get_roles = lambda _user: []
        with self.assertRaisesRegex(ValueError, "School Admin"):
            cancellation.cancel_by_admin("PB-1", reason="reason", request_key="k", admin=True)
        self.fake.get_roles = lambda _user: ["School Admin"]
        with patch.object(cancellation, "get_support_view_token", return_value="token"):
            with self.assertRaisesRegex(ValueError, "Support View"):
                cancellation.cancel_by_admin("PB-1", reason="reason", request_key="k")
        self.assertEqual(self.entries("Return"), [])

    def test_lock_is_idempotent_and_absent_does_not_return(self):
        self.db.exists.side_effect = lambda dt, filters: bool(self.entries("Consume")) if filters.get("kind") == "Consume" else False
        first = cancellation.lock_due_booking("PB-1")
        second = cancellation.lock_due_booking("PB-1")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.entries("Consume")), 1)
        self.assertEqual(self.entries("Return"), [])
        self.assertEqual(self.attendance.status, "Absent")
        self.assertEqual(self.state["docs"][("QAS PAYG Booking", "PB-1")].status, "Locked")

    def test_cancel_before_lock_then_lock_is_noop(self):
        cancellation.cancel_by_admin("PB-1", reason="School closure", request_key="k")
        self.assertFalse(cancellation.lock_due_booking("PB-1"))
        self.assertEqual((len(self.entries("Return")), len(self.entries("Consume"))), (1, 0))

    def test_lock_then_admin_cancel_returns_consumed_unit_only(self):
        self.assertTrue(cancellation.lock_due_booking("PB-1"))
        cancellation.cancel_by_admin("PB-1", reason="Correction", request_key="k")
        self.assertEqual((len(self.entries("Consume")), len(self.entries("Return"))), (1, 1))
        self.assertEqual((self.card.available_count, self.card.reserved_count,
                          self.card.consumed_count), (10, 0, 0))
        self.assertFalse(any(dt in {"Voucher", "Store Credit", "Sales Invoice"}
                             for dt, _ in self.state["docs"]))

    def test_aware_utc_now_matches_naive_brisbane_deadline(self):
        cancellation._now.return_value = datetime(2026, 9, 23, 23, 59, tzinfo=timezone.utc)
        self.assertFalse(cancellation.lock_due_booking("PB-1"))
        cancellation._now.return_value = datetime(2026, 9, 24, 0, tzinfo=timezone.utc)
        self.assertTrue(cancellation.lock_due_booking("PB-1"))
        self.assertEqual(len(self.entries("Consume")), 1)

    def test_leave_reserved_admin_cancel_returns_reserved_unit(self):
        self.attendance.status = "Leave"
        cancellation.session_resources.active_rows.return_value = []
        cancellation.cancel_by_admin("PB-1", reason="Leave correction", request_key="leave-r")
        returned = self.entries("Return")[0]
        self.assertEqual((returned.available_delta, returned.reserved_delta,
                          returned.consumed_delta), (1, -1, 0))
        self.assertEqual(self.attendance.status, "Cancelled")
        self.assertEqual(self.attendance.source_document, "PB-1")

    def test_leave_attendance_locks_then_admin_returns_original_card(self):
        self.attendance.status = "Leave"
        cancellation.session_resources.active_rows.return_value = []
        self.assertTrue(cancellation.lock_due_booking("PB-1"))
        cancellation.cancel_by_admin("PB-1", reason="Leave correction", request_key="leave-1")
        self.assertEqual((self.card.available_count, self.card.reserved_count,
                          self.card.consumed_count), (10, 0, 0))
        self.assertEqual(self.attendance.status, "Cancelled")
        self.assertEqual(self.attendance.marked_by, "teacher@example.com")
        self.assertEqual(self.attendance.marked_at, datetime(2026, 9, 27, 11))
        self.assertLess(self.events.index("Classroom:ROOM-1"),
                        self.events.index("Class Attendance Entry:ATT-1"))

    def test_current_attendance_must_belong_to_booking(self):
        for field, bad in (("student", "OTHER"), ("course_session", "CS-OTHER"),
                           ("source_doctype", "Adhoc Booking"), ("source_document", "OTHER")):
            original = self.attendance[field]
            self.attendance[field] = bad
            with self.assertRaisesRegex(ValueError, "attendance.*match"):
                cancellation.lock_due_booking("PB-1")
            self.attendance[field] = original
        self.assertEqual(self.entries("Consume"), [])

    def test_cancelled_attendance_with_open_booking_is_consistency_error(self):
        self.attendance.status = "Cancelled"
        cancellation.session_resources.active_rows.return_value = []
        with self.assertRaisesRegex(ValueError, "attendance.*Cancelled"):
            cancellation.lock_due_booking("PB-1")
        with self.assertRaisesRegex(ValueError, "attendance.*Cancelled"):
            cancellation.cancel_by_admin("PB-1", reason="Correction", request_key="k")
        self.assertEqual(self.entries("Return"), [])

    def test_admin_cancelled_leave_cannot_be_reopened_by_teacher_mark(self):
        self.attendance.status = "Leave"
        cancellation.session_resources.active_rows.return_value = []
        cancellation.cancel_by_admin("PB-1", reason="Leave correction", request_key="leave-k")
        old_value = self.db.get_value.side_effect
        def value(dt, name, fields, **kwargs):
            if dt == "Class Attendance Entry" and isinstance(fields, list):
                return frappe._dict(student="S-1", course_session="CS-1")
            return old_value(dt, name, fields, **kwargs)
        self.db.get_value.side_effect = value
        def access(**kwargs):
            if kwargs["row"] is None:
                return
            teacher_portal._is_blocked_teacher_attendance_update(
                "CS-1", "ATT-1", {"status": "Present", "comments": ""},
                current=kwargs["row"],
            )
        with patch.object(attendance_commands, "frappe", self.fake), \
             patch.object(attendance_commands, "_validate_status"), \
             patch.object(teacher_portal, "frappe", self.fake):
            with self.assertRaisesRegex(ValueError, "Teachers cannot change"):
                attendance_commands.update_attendance_status(
                    "CS-1", "ATT-1", "Present", validate_access=access,
                )
        self.assertEqual(self.attendance.status, "Cancelled")
        self.assertEqual(len(self.entries("Return")), 1)

    def test_scheduler_advances_cursor_and_isolates_failed_booking(self):
        self.fake.log_error = self.db.rollback.__class__()
        self.fake.get_traceback = lambda: "Traceback: temporary failure"
        rows = iter([[frappe._dict(name="PB-1", cancellable_until="2026-09-24 09:00:00")],
                     [frappe._dict(name="PB-2", cancellable_until="2026-09-24 10:00:00")], []])
        self.db.sql.side_effect = lambda query, _params, **_kwargs: next(rows)
        def process(name):
            if name == "PB-1":
                raise RuntimeError("temporary failure")
            return True
        with patch.object(payg_booking_tasks, "lock_due_booking", side_effect=process):
            result = payg_booking_tasks._lock_due_payg_bookings(batch_size=1)
        self.assertEqual(result["locked"], ["PB-2"])
        self.assertEqual(self.db.commit.call_count, 1)
        self.db.rollback.assert_called_once_with()
        self.assertIn("Traceback: temporary failure", str(self.fake.log_error.call_args))
        self.assertEqual(self.db.sql.call_args_list[1].args[1][1:],
                         ("2026-09-24 09:00:00", "2026-09-24 09:00:00", "PB-1", 1))
