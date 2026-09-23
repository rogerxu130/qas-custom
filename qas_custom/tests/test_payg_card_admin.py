"""Site-free contracts for audited PAYG card administration.

The savepoint assertions here do not replace Task 9's real MariaDB concurrency tests.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.payg import card_admin
from qas_custom.tests.test_payg_issue import Document


class TestCardAdmin(TestCase):
    def setUp(self):
        self.docs = {}
        self.events = []
        self.add("Parent", "P", customer="C")
        self.add("Student", "S", guardian="P")
        self.add("Course", "OLD")
        self.add("Course", "NEW")
        self.add("QAS PAYG Product", "PROD-OLD", course="OLD", enabled=1,
                 standard_card_price=Decimal("400"))
        self.add("QAS PAYG Product", "PROD-NEW", course="NEW", enabled=1,
                 standard_card_price=Decimal("550"))
        self.card = self.add("QAS PAYG Card", "CARD", family_parent="P", customer="C",
                             product="PROD-OLD", course="OLD", status="Active",
                             issued_on=date(2026, 9, 1), expires_on=date(2027, 3, 1),
                             unit_price_snapshot=Decimal("40"), available_count=7,
                             reserved_count=0, consumed_count=3)
        self.add("Course Sessions", "CS", session_date=date(2026, 10, 1), weekly_timeslot="W")
        self.add("Weekly Timeslot", "W", start_time="10:00")
        self.fake = frappe._dict(
            session=frappe._dict(user="admin@example.com"),
            get_roles=lambda _user: ["School Admin"], get_doc=Mock(side_effect=self.get_doc),
            db=frappe._dict(sql=Mock(side_effect=self.sql), get_value=Mock(side_effect=self.value),
                            savepoint=Mock(), rollback=Mock()),
            throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
            PermissionError=PermissionError, DuplicateEntryError=frappe.DuplicateEntryError,
            UniqueValidationError=frappe.UniqueValidationError,
        )
        for item in (patch.object(card_admin, "frappe", self.fake),
                     patch.object(card_admin, "get_support_view_token", return_value=""),
                     patch.object(card_admin, "_now", return_value=datetime(2026, 9, 23, 12))):
            item.start(); self.addCleanup(item.stop)

    def add(self, doctype, name, **fields):
        doc = Document({"docs": self.docs}, doctype=doctype, name=name, **fields)
        self.docs[(doctype, name)] = doc
        return doc

    def get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            return Document({"docs": self.docs}, **doctype)
        if kwargs.get("for_update"):
            self.events.append(f"{doctype}:{name}")
        return self.docs[(doctype, name)]

    def value(self, doctype, name, field, **_kwargs):
        doc = self.docs.get((doctype, name))
        if not doc:
            return None
        if isinstance(field, (list, tuple)):
            return frappe._dict({key: doc.get(key) for key in field})
        return doc.get(field)

    def sql(self, query, params, **_kwargs):
        if "tabStudent" in query:
            self.events.append("Student:S")
            return [frappe._dict(name="S")]
        if "tabParent" in query:
            self.events.append("Parent:P")
            return [frappe._dict(name="P")]
        if "tabQAS PAYG Booking" in query:
            return [frappe._dict(name=doc.name, student=doc.student)
                    for (kind, _), doc in self.docs.items()
                    if kind == "QAS PAYG Booking" and doc.card == params[0]
                    and doc.status != "Cancelled"]
        if "tabQAS PAYG Operation" in query:
            return [frappe._dict(name=doc.name) for (kind, _), doc in self.docs.items()
                    if kind == "QAS PAYG Operation" and doc.operation_type == params[0]
                    and doc.request_key == params[1]]
        return []

    def entries(self):
        return [doc for (kind, _), doc in self.docs.items() if kind == "QAS PAYG Entry"]

    def test_expiry_change_audits_only_date_and_is_idempotent(self):
        old = self.card.expires_on
        op = card_admin.change_expiry("CARD", date(2027, 4, 1),
                                      reason="Parent request", request_key="extend-1")
        self.assertEqual((op.operation_type, op.card, op.old_expiry, op.new_expiry, op.reason),
                         ("ExpiryChange", "CARD", old, date(2027, 4, 1), "Parent request"))
        self.assertEqual(self.card.expires_on, date(2027, 4, 1))
        self.assertEqual((self.card.available_count, self.card.consumed_count), (7, 3))
        self.assertEqual(self.entries(), [])
        self.assertIs(card_admin.change_expiry("CARD", date(2027, 4, 1),
                                              reason="Parent request", request_key="extend-1"), op)
        with self.assertRaisesRegex(ValueError, "request key"):
            card_admin.change_expiry("CARD", date(2027, 5, 1),
                                     reason="Different", request_key="extend-1")
        self.assertLess(self.events.index("Student:S"), self.events.index("Parent:P"))

    def test_absent_operation_key_lookup_does_not_gap_lock(self):
        self.assertIsNone(card_admin._operation_by_key("Exchange", "missing"))
        query = self.fake.db.sql.call_args.args[0]
        self.assertIn("tabQAS PAYG Operation", query)
        self.assertNotIn("FOR UPDATE", query)

    def test_shrinking_expiry_lists_non_cancelled_booking_and_rechecks_under_lock(self):
        self.add("QAS PAYG Booking", "B-1", card="CARD", student="S", status="Reserved",
                 course_session="CS")
        with self.assertRaisesRegex(ValueError, "B-1"):
            card_admin.change_expiry("CARD", date(2026, 9, 30),
                                     reason="Correction", request_key="shrink-1")
        self.assertEqual(self.card.expires_on, date(2027, 3, 1))
        self.assertEqual(self.fake.db.rollback.call_count, 1)
        self.assertLess(self.events.index("Student:S"), self.events.index("QAS PAYG Booking:B-1"))
        self.docs[("QAS PAYG Booking", "B-1")].status = "Cancelled"
        card_admin.change_expiry("CARD", date(2026, 9, 30),
                                 reason="Correction", request_key="shrink-1")

    def test_all_session_timeslot_term_and_room_locks_have_stable_order(self):
        self.add("QAS PAYG Booking", "B-1", card="CARD", student="S", status="Reserved",
                 course_session="CS-Z")
        self.add("QAS PAYG Booking", "B-2", card="CARD", student="S", status="Reserved",
                 course_session="CS-A")
        self.add("Course Sessions", "CS-Z", session_date=date(2026, 10, 2), weekly_timeslot="W-A")
        self.add("Course Sessions", "CS-A", session_date=date(2026, 10, 3), weekly_timeslot="W-Z")
        self.add("Weekly Timeslot", "W-Z", start_time="10:00", term="T-A", classroom="R-Z")
        self.add("Weekly Timeslot", "W-A", start_time="10:00", term="T-Z", classroom="R-A")
        for kind, names in (("Term", ("T-A", "T-Z")), ("Classroom", ("R-A", "R-Z"))):
            for name in names:
                self.add(kind, name)
        card_admin.change_expiry("CARD", date(2027, 4, 1),
                                 reason="Extension", request_key="order-1")
        locked = [event for event in self.events if event.startswith((
            "Course Sessions:", "Weekly Timeslot:", "Term:", "Classroom:"))]
        self.assertEqual(locked, ["Course Sessions:CS-A", "Course Sessions:CS-Z",
                                  "Weekly Timeslot:W-A", "Weekly Timeslot:W-Z",
                                  "Term:T-A", "Term:T-Z", "Classroom:R-A", "Classroom:R-Z"])

    def test_exchange_positive_delta_transfers_all_available(self):
        op = card_admin.exchange_card("CARD", "PROD-NEW", "exchange-1")
        target = self.docs[("QAS PAYG Card", op.target_card)]
        self.assertEqual((op.quantity, op.old_price, op.new_price, op.price_delta),
                         (7, Decimal("40"), Decimal("55"), Decimal("105")))
        self.assertEqual((op.source_card, op.new_course), ("CARD", "NEW"))
        self.assertEqual((self.card.available_count, self.card.consumed_count,
                          self.card.status, self.card.expires_on),
                         (0, 3, "Transferred", date(2027, 3, 1)))
        self.assertEqual((target.available_count, target.reserved_count, target.consumed_count,
                          target.issued_on, target.expires_on, target.unit_price_snapshot),
                         (7, 0, 0, date(2026, 9, 23), date(2027, 3, 23), Decimal("55")))
        self.assertEqual([(e.kind, e.available_delta, e.operation_key) for e in self.entries()],
                         [("Transfer Out", -7, f"transfer-out:{op.name}"),
                          ("Transfer In", 7, f"transfer-in:{op.name}")])
        self.assertIs(card_admin.exchange_card("CARD", "PROD-NEW", "exchange-1"), op)
        self.assertEqual(len(self.entries()), 2)
        with self.assertRaisesRegex(ValueError, "request key"):
            card_admin.exchange_card("CARD", "PROD-OLD", "exchange-1")
        with self.assertRaisesRegex(ValueError, "request key"):
            card_admin.exchange_card("CARD", "PROD-NEW", "exchange-1", at="2026-09-24")

    def test_explicit_exchange_date_uses_current_read_and_rejects_missing_target(self):
        op = card_admin.exchange_card("CARD", "PROD-NEW", "date-1", at="2026-09-23")
        original = self.fake.db.get_value.side_effect
        def stale_value(doctype, name, field, **kwargs):
            if doctype == "QAS PAYG Card" and name == op.target_card and field == "issued_on":
                return original(doctype, name, field) if kwargs.get("for_update") else None
            return original(doctype, name, field, **kwargs)
        self.fake.db.get_value.side_effect = stale_value
        self.assertIs(card_admin.exchange_card("CARD", "PROD-NEW", "date-1", at="2026-09-23"), op)
        self.assertTrue(any(call.args[:3] == ("QAS PAYG Card", op.target_card, "issued_on")
                            and call.kwargs.get("for_update")
                            for call in self.fake.db.get_value.call_args_list))
        del self.docs[("QAS PAYG Card", op.target_card)]
        with self.assertRaisesRegex(ValueError, "target card"):
            card_admin.exchange_card("CARD", "PROD-NEW", "date-1", at="2026-09-23")

    def test_only_operation_insert_collision_reads_winner(self):
        original = self.fake.get_doc.side_effect
        class CollidingOperation(Document):
            def insert(inner, **_kwargs):
                self.add("QAS PAYG Operation", "WIN", operation_type="Exchange",
                         request_key="collision", source_card="CARD", product="PROD-NEW",
                         target_card="TARGET", status="Completed")
                raise frappe.DuplicateEntryError("operation key collision")
        def collide(doctype, name=None, **kwargs):
            if isinstance(doctype, dict) and doctype.get("doctype") == "QAS PAYG Operation":
                return CollidingOperation({"docs": self.docs}, **doctype)
            return original(doctype, name, **kwargs)
        self.fake.get_doc.side_effect = collide
        winner = card_admin.exchange_card("CARD", "PROD-NEW", "collision")
        self.assertEqual(winner.name, "WIN")
        lookups = [call.args[0] for call in self.fake.db.sql.call_args_list
                   if "tabQAS PAYG Operation" in call.args[0]]
        self.assertEqual(len(lookups), 2)
        self.assertNotIn("FOR UPDATE", lookups[0])
        self.assertIn("FOR UPDATE", lookups[1])
        self.assertEqual(self.fake.db.rollback.call_count, 1)
        self.assertEqual(self.entries(), [])

    def test_expiry_operation_collision_reuses_only_matching_winner(self):
        original = self.fake.get_doc.side_effect
        class CollidingOperation(Document):
            def insert(inner, **_kwargs):
                self.add("QAS PAYG Operation", "WIN-EXPIRY", operation_type="ExpiryChange",
                         request_key="expiry-collision", card="CARD",
                         new_expiry=date(2027, 4, 1), reason="Extension", status="Completed")
                raise frappe.UniqueValidationError("operation key collision")
        def collide(doctype, name=None, **kwargs):
            if isinstance(doctype, dict) and doctype.get("doctype") == "QAS PAYG Operation":
                return CollidingOperation({"docs": self.docs}, **doctype)
            return original(doctype, name, **kwargs)
        self.fake.get_doc.side_effect = collide
        winner = card_admin.change_expiry("CARD", date(2027, 4, 1),
                                          reason="Extension", request_key="expiry-collision")
        self.assertEqual(winner.name, "WIN-EXPIRY")
        self.assertEqual(self.card.expires_on, date(2027, 3, 1))
        lookups = [call.args[0] for call in self.fake.db.sql.call_args_list
                   if "tabQAS PAYG Operation" in call.args[0]]
        self.assertEqual(len(lookups), 2)
        self.assertNotIn("FOR UPDATE", lookups[0])
        self.assertIn("FOR UPDATE", lookups[1])

    def test_entry_unique_failure_is_not_treated_as_operation_collision(self):
        original = self.fake.get_doc.side_effect
        def collide_entry(doctype, name=None, **kwargs):
            if isinstance(doctype, dict) and doctype.get("kind") == "Transfer In":
                raise frappe.DuplicateEntryError("entry key collision")
            return original(doctype, name, **kwargs)
        self.fake.get_doc.side_effect = collide_entry
        with self.assertRaises(frappe.DuplicateEntryError):
            card_admin.exchange_card("CARD", "PROD-NEW", "entry-failure")
        lookups = [call.args[0] for call in self.fake.db.sql.call_args_list
                   if "tabQAS PAYG Operation" in call.args[0]]
        self.assertEqual(len(lookups), 1)
        self.assertEqual(self.fake.db.rollback.call_count, 1)

    def test_exchange_quantizes_nine_decimal_currency_before_persistence(self):
        self.docs[("QAS PAYG Product", "PROD-NEW")].standard_card_price = Decimal("1.000000001")
        self.card.unit_price_snapshot = Decimal("0.099999999")
        positive = card_admin.exchange_card("CARD", "PROD-NEW", "fraction-positive")
        self.assertEqual((positive.old_price, positive.new_price, positive.price_delta),
                         (Decimal("0.099999999"), Decimal("0.100000000"), Decimal("0.000000007")))
        self.assertEqual(self.docs[("QAS PAYG Card", positive.target_card)].unit_price_snapshot,
                         positive.new_price)
        self.add("QAS PAYG Card", "CARD2", family_parent="P", customer="C",
                 product="PROD-OLD", course="OLD", status="Active",
                 issued_on=date(2026, 9, 1), expires_on=date(2027, 3, 1),
                 unit_price_snapshot=Decimal("0.100000001"), available_count=1,
                 reserved_count=0, consumed_count=9)
        negative = card_admin.exchange_card("CARD2", "PROD-NEW", "fraction-negative")
        self.assertEqual((negative.new_price, negative.price_delta),
                         (Decimal("0.100000000"), Decimal("-0.000000001")))

    def test_exchange_negative_zero_and_late_return_reexchange(self):
        self.docs[("QAS PAYG Product", "PROD-NEW")].standard_card_price = Decimal("300")
        op = card_admin.exchange_card("CARD", "PROD-NEW", "exchange-negative")
        self.assertEqual(op.price_delta, Decimal("-70"))
        self.assertFalse(op.get("invoice"))
        # The Task 5 cancellation command returns to this source, not its target.
        # Simulate that ledger effect to verify Task 6 allows another whole transfer.
        self.card.available_count = 1
        self.docs[("QAS PAYG Product", "PROD-NEW")].standard_card_price = Decimal("400")
        again = card_admin.exchange_card("CARD", "PROD-NEW", "exchange-returned")
        self.assertEqual((again.quantity, again.price_delta, self.card.status),
                         (1, Decimal("0"), "Transferred"))
        self.assertEqual(self.docs[("QAS PAYG Card", again.target_card)].available_count, 1)

    def test_exchange_rejects_future_booking_and_permissions(self):
        booking = self.add("QAS PAYG Booking", "B-1", card="CARD", student="S",
                           status="Reserved", course_session="CS")
        with self.assertRaisesRegex(ValueError, "B-1"):
            card_admin.exchange_card("CARD", "PROD-NEW", "blocked")
        self.assertEqual(self.card.available_count, 7)
        booking.status = "Cancelled"
        self.fake.get_roles = lambda _user: []
        with self.assertRaisesRegex(ValueError, "School Admin"):
            card_admin.exchange_card("CARD", "PROD-NEW", "blocked")

    def test_support_view_and_invalid_target_are_rejected(self):
        with patch.object(card_admin, "get_support_view_token", return_value="view-token"):
            with self.assertRaisesRegex(ValueError, "Support View"):
                card_admin.change_expiry("CARD", date(2027, 4, 1), reason="x", request_key="x")
        self.docs[("QAS PAYG Product", "PROD-NEW")].enabled = 0
        with self.assertRaisesRegex(ValueError, "enabled product"):
            card_admin.exchange_card("CARD", "PROD-NEW", "invalid")
        self.assertFalse(any(kind == "QAS PAYG Operation" for kind, _ in self.docs))

    def test_exchange_requires_positive_whole_available_count(self):
        self.card.available_count = 0
        with self.assertRaisesRegex(ValueError, "1 to 10"):
            card_admin.exchange_card("CARD", "PROD-NEW", "empty")
        self.card.available_count = 11
        with self.assertRaisesRegex(ValueError, "1 to 10"):
            card_admin.exchange_card("CARD", "PROD-NEW", "overfull")

    def test_transfer_failure_rolls_back_savepoint(self):
        original = self.fake.get_doc.side_effect
        def fail_on_in(doctype, name=None, **kwargs):
            if isinstance(doctype, dict) and doctype.get("kind") == "Transfer In":
                raise RuntimeError("in failed")
            return original(doctype, name, **kwargs)
        self.fake.get_doc.side_effect = fail_on_in
        with self.assertRaisesRegex(RuntimeError, "in failed"):
            card_admin.exchange_card("CARD", "PROD-NEW", "fail")
        self.fake.db.rollback.assert_called_once()
        self.assertEqual(self.fake.db.rollback.call_args.kwargs.keys(), {"save_point"})
