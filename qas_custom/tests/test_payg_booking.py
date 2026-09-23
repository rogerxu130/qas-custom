"""Site-free PAYG command tests.

Mocks check call order and savepoint use; they do not prove actual MariaDB rollback,
row-lock serialization, or concurrent last-seat/last-unit correctness. Task 9 must
exercise these cases on a migrated site with independent database connections.
"""
from datetime import datetime, date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.payg import booking
from qas_custom.modules.payg.rules import PreviewStale
from qas_custom.tests.test_payg_issue import Document


class TestBooking(TestCase):
    def setUp(self):
        self.state = {"docs": {}}
        self.events = []
        self.student = self.add("Student", "S-1", guardian="P-1", date_of_birth="2018-01-01", status="Active")
        self.add("Course", "C-1", min_age=5, max_age=12, status="Active")
        self.add("Course Sessions", "CS-1", weekly_timeslot="W-1", session_date="2026-09-27", status="Scheduled")
        self.add("Weekly Timeslot", "W-1", term="T-1", course="C-1", classroom="ROOM-1",
                 teacher="TEACHER-1", status="Active", day_of_week="Sunday",
                 start_time="10:00", end_time="11:00")
        self.add("Term", "T-1", status="Active", start_date="2026-09-01", end_date="2026-12-31")
        self.add("Classroom", "ROOM-1", capacity=10)
        self.card = self.add("QAS PAYG Card", "A", family_parent="P-1", course="C-1", status="Active",
                             issued_on=date(2026, 9, 1), expires_on=date(2027, 3, 1),
                             available_count=10)
        self.db = SimpleNamespace(sql=Mock(side_effect=self.sql), get_value=Mock(side_effect=self.value),
                                  savepoint=Mock(), rollback=Mock(), set_value=Mock(), exists=Mock(return_value=False))
        self.fake = SimpleNamespace(session=SimpleNamespace(user="parent@example.com"), db=self.db,
                                    get_doc=Mock(side_effect=self.get_doc), get_all=Mock(side_effect=self.get_all),
                                    throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
                                    PermissionError=PermissionError,
                                    DuplicateEntryError=frappe.DuplicateEntryError,
                                    UniqueValidationError=frappe.UniqueValidationError)
        self.patches = [patch.object(booking, "frappe", self.fake),
                        patch.object(booking, "require_parent", return_value=SimpleNamespace(name="P-1")),
                        patch.object(booking, "get_support_view_token", return_value=""),
                        patch.object(booking, "_now", return_value=datetime(2026, 9, 24, 10)),
                        patch.object(booking.session_resources, "reserve_regular_place", side_effect=self.reserve),
                        patch.object(booking.session_resources, "active_rows", return_value=[]),
                        patch.object(booking.session_resources, "student_has_conflict", return_value=False),
                        patch.object(booking.session_resources, "classroom_capacity", return_value=10)]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def add(self, dt, name, **fields):
        doc = Document(self.state, doctype=dt, name=name, **fields)
        self.state["docs"][(dt, name)] = doc
        return doc

    def get_doc(self, dt, name=None, **kwargs):
        if isinstance(dt, dict):
            return Document(self.state, **dt)
        if kwargs.get("for_update"):
            self.events.append(dt + ":" + name)
        return self.state["docs"][(dt, name)]

    def get_all(self, dt, filters=None, pluck=None, **_kwargs):
        docs = [doc for (kind, _), doc in self.state["docs"].items() if kind == dt and
                all(doc.get(key) == value for key, value in (filters or {}).items())]
        return [doc.name for doc in docs] if pluck == "name" else docs

    def sql(self, query, params, **_kwargs):
        if "tabStudent" in query and "FOR UPDATE" in query:
            self.events.append("Student:" + params[0])
            return [self.state["docs"][("Student", params[0])]]
        if "tabQAS PAYG Booking" in query and "request_key" in query:
            return [frappe._dict(name=doc.name) for (dt, _), doc in self.state["docs"].items()
                    if dt == "QAS PAYG Booking" and doc.request_key == params[0]]
        if "tabQAS PAYG Card" in query and "FOR UPDATE" in query:
            return [frappe._dict(name=doc.name) for (dt, _), doc in self.state["docs"].items()
                    if dt == "QAS PAYG Card" and doc.family_parent == params[0]
                    and (len(params) == 1 or doc.course == params[1])]
        if "FROM `tabCourse Sessions` s" in query:
            return [frappe._dict(name="CS-1", session_date="2026-09-27")]
        return []

    def value(self, dt, key, field, **_kwargs):
        if dt == "QAS PAYG Booking" and isinstance(key, dict):
            return next((doc.name for (kind, _), doc in self.state["docs"].items()
                         if kind == dt and doc.request_key == key["request_key"]), None)
        if dt == "Course Sessions":
            doc = self.state["docs"][(dt, key)]
            if isinstance(field, list): return frappe._dict({name: doc.get(name) for name in field})
            return doc.get(field)
        if dt == "Weekly Timeslot": return self.state["docs"][(dt, key)].get(field)
        if dt == "QAS PAYG Booking": return self.state["docs"][(dt, key)].get(field)
        return None

    def reserve(self, student, session, _dt, source, _kind, *, validate_business):
        self.events.append("Session:" + session)
        validate_business(self.state["docs"][("Course Sessions", session)],
                          self.state["docs"][("Weekly Timeslot", "W-1")],
                          self.state["docs"][("Term", "T-1")], [])
        return "ATT-NEW"



    def test_locked_student_uses_current_fields_not_cached_document(self):
        stale = self.add("Student", "STALE", guardian="OTHER", status="Inactive",
                         date_of_birth="2025-01-01")
        original = self.fake.get_doc.side_effect
        self.fake.get_doc.side_effect = lambda dt, name=None, **kw: stale if dt == "Student" else original(dt, name, **kw)
        current = booking._student("S-1", "P-1", lock=True)
        self.assertEqual((current.guardian, current.status, current.date_of_birth),
                         ("P-1", "Active", "2018-01-01"))
        self.assertTrue(any("guardian, status, date_of_birth" in call.args[0]
                            and "FOR UPDATE" in call.args[0] for call in self.db.sql.call_args_list))

    def test_request_key_current_read_beats_old_snapshot(self):
        existing = self.add("QAS PAYG Booking", "PB-EXIST", family_parent="P-1",
                            student="S-1", course_session="CS-1", request_key="req-1")
        self.db.get_value.side_effect = lambda dt, key, field, **kw: "PB-EXIST" if dt == "QAS PAYG Booking" and isinstance(key, dict) else self.value(dt, key, field, **kw)
        found = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertIs(found, existing)
        self.assertFalse(any("tabQAS PAYG Booking" in call.args[0]
                             for call in self.db.sql.call_args_list))
        self.assertLess(self.events.index("Student:S-1"), self.events.index("QAS PAYG Booking:PB-EXIST"))

    def test_duplicate_retry_rechecks_request_key_with_lock(self):
        original = self.fake.get_doc.side_effect
        test = self
        class CollidingBooking(Document):
            def insert(self, **_options):
                test.add("QAS PAYG Booking", "PB-OTHER", family_parent="P-1",
                         student="S-1", course_session="CS-1", request_key="req-1")
                raise frappe.DuplicateEntryError("duplicate")
        def race(dt, name=None, **kwargs):
            if isinstance(dt, dict) and dt.get("doctype") == "QAS PAYG Booking":
                return CollidingBooking(test.state, **dt)
            return original(dt, name, **kwargs)
        self.fake.get_doc.side_effect = race
        found = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertEqual(found.name, "PB-OTHER")
        self.db.rollback.assert_called_once()
        reads = [call for call in self.db.sql.call_args_list if "tabQAS PAYG Booking" in call.args[0]]
        self.assertEqual(len(reads), 1)
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in reads))

    def test_card_name_discovery_is_current_and_sorted(self):
        self.add("QAS PAYG Card", "B", family_parent="P-1", course="C-1", status="Active")
        self.fake.get_all.side_effect = AssertionError("stale snapshot")
        cards = booking._cards("P-1", "C-1", lock=True)
        self.assertEqual([card.name for card in cards], ["A", "B"])
        query = next(call.args[0] for call in self.db.sql.call_args_list if "tabQAS PAYG Card" in call.args[0])
        self.assertIn("ORDER BY name FOR UPDATE", query)
        self.assertEqual(self.events[-2:], ["QAS PAYG Card:A", "QAS PAYG Card:B"])


    def test_missing_request_key_does_not_take_gap_lock(self):
        booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertFalse(any("tabQAS PAYG Booking" in call.args[0]
                             for call in self.db.sql.call_args_list))
        self.assertTrue(any(call.args[0] == "QAS PAYG Booking"
                            and isinstance(call.args[1], dict)
                            for call in self.db.get_value.call_args_list))


    def test_later_entry_unique_error_never_locks_missing_booking_key(self):
        original = self.fake.get_doc.side_effect
        class CollidingEntry(Document):
            def insert(self, **_options):
                raise frappe.DuplicateEntryError("entry key")
        def get_doc(dt, name=None, **kwargs):
            if isinstance(dt, dict) and dt.get("doctype") == "QAS PAYG Entry":
                return CollidingEntry(self.state, **dt)
            return original(dt, name, **kwargs)
        self.fake.get_doc.side_effect = get_doc
        with self.assertRaises(frappe.DuplicateEntryError):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.db.rollback.assert_called_once()
        self.assertFalse(any("tabQAS PAYG Booking" in call.args[0]
                             for call in self.db.sql.call_args_list))

    def test_new_booking_carries_short_lived_locked_context(self):
        original = self.fake.get_doc.side_effect
        seen = []
        class CheckedBooking(Document):
            def insert(self, **options):
                seen.append((options, dict(self.flags.payg_create_context)))
                return super().insert(**options)
        def get_doc(dt, name=None, **kwargs):
            if isinstance(dt, dict) and dt.get("doctype") == "QAS PAYG Booking":
                return CheckedBooking(self.state, **dt)
            return original(dt, name, **kwargs)
        self.fake.get_doc.side_effect = get_doc
        saved = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0]["ignore_links"])
        self.assertEqual((seen[0][1]["student"], seen[0][1]["card_family"]), ("S-1", "P-1"))
        self.assertIsNone(saved.flags.payg_create_context)
        self.assertFalse(saved.flags.ignore_links)

    def test_rules_must_be_explicitly_confirmed(self):
        with self.assertRaisesRegex(ValueError, "confirm.*rules"):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1")
        self.db.savepoint.assert_not_called()
        result = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertEqual(result.status, "Reserved")


    def test_age_filter_advances_cursor_across_raw_batches(self):
        course = self.state["docs"][("Course", "C-1")]
        course.min_age, course.max_age = 10, 15
        self.add("Course Sessions", "CS-2", weekly_timeslot="W-1", session_date="2029-09-30", status="Scheduled")
        self.add("Course Sessions", "CS-3", weekly_timeslot="W-1", session_date="2030-09-29", status="Scheduled")
        self.state["docs"][("Term", "T-1")].end_date = "2031-12-31"
        rows = [frappe._dict(name="CS-1", session_date="2026-09-27"),
                frappe._dict(name="CS-2", session_date="2029-09-30"),
                frappe._dict(name="CS-3", session_date="2030-09-29")]
        original = self.sql
        def paged(query, params, **kwargs):
            if "ORDER BY s.session_date" in query:
                after = (str(params[1]), params[3])
                return [r for r in rows if (r.session_date, r.name) > after][:params[-1]]
            return original(query, params, **kwargs)
        self.db.sql.side_effect = paged
        result = booking.available_sessions("S-1", "C-1", limit=1)
        self.assertEqual([item["name"] for item in result["items"]], ["CS-2"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], ("2029-09-30", "CS-2"))

    def test_ndis_preview_uses_shared_effective_capacity(self):
        self.state["docs"][("Weekly Timeslot", "W-1")].ndis_friendly = 1
        booking.session_resources.classroom_capacity.return_value = 20
        booking.session_resources.active_rows.return_value = [
            frappe._dict(student="S-2"), frappe._dict(student="S-3")]
        with patch("qas_custom.services.ndis_friendly.NDIS_FRIENDLY_CAPACITY", 2):
            result = booking.preview_booking("S-1", "CS-1")
        self.assertIn("full", result["reasons"])
        with patch("qas_custom.services.ndis_friendly.NDIS_FRIENDLY_CAPACITY", 2):
            listed = booking.available_sessions("S-1", "C-1")
        self.assertIn("full", listed["items"][0]["reasons"])

    def test_age_mismatch_filtered_but_unknown_birth_is_visible_unbookable(self):
        self.student.date_of_birth = "2024-01-01"
        self.assertEqual(booking.available_sessions("S-1", "C-1")["items"], [])
        self.student.date_of_birth = "2000-01-01"
        self.assertEqual(booking.available_sessions("S-1", "C-1")["items"], [])
        self.student.date_of_birth = None
        result = booking.available_sessions("S-1", "C-1")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["reasons"], ["student_age_unknown"])

    def test_two_children_same_card_spend_separate_reservations(self):
        self.add("Student", "S-2", guardian="P-1", date_of_birth="2018-01-01", status="Active")
        first = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        second = booking.confirm_booking("S-2", "CS-1", "A", "req-2", confirmed_rules=True)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(len([d for (dt, _), d in self.state["docs"].items()
                              if dt == "QAS PAYG Entry" and d.kind == "Reserve"]), 2)
        self.assertLess(self.events.index("Student:S-1"), self.events.index("QAS PAYG Card:A"))
        self.assertLess(self.events.index("QAS PAYG Card:A"), self.events.index("Session:CS-1"))
        self.assertEqual(first.attendance_entry, "ATT-NEW")
        self.assertEqual((self.card.available_count, self.card.reserved_count), (8, 2))

    def test_support_view_write_is_rejected_without_side_effect(self):
        booking.get_support_view_token.return_value = "support-token"
        with self.assertRaisesRegex(ValueError, "Support View"):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.db.savepoint.assert_not_called()

    def test_other_family_stale_preview_and_required_preview(self):
        self.student.guardian = "OTHER"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.student.guardian = "P-1"
        self.add("QAS PAYG Card", "B", family_parent="P-1", course="C-1", status="Active",
                 issued_on=date(2026, 9, 1), expires_on=date(2027, 2, 1), available_count=10)
        with self.assertRaises(PreviewStale):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        with self.assertRaisesRegex(ValueError, "preview card"):
            booking.confirm_booking("S-1", "CS-1", "", "req-1", confirmed_rules=True)
        self.assertFalse(any(dt == "QAS PAYG Booking" for dt, _ in self.state["docs"]))

    def test_different_request_cannot_spend_last_unit(self):
        self.card.available_count = 1
        booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.add("Student", "S-2", guardian="P-1", date_of_birth="2018-01-01", status="Active")
        with self.assertRaises(PreviewStale):
            booking.confirm_booking("S-2", "CS-1", "A", "req-2", confirmed_rules=True)
        self.assertEqual(len([dt for dt, _ in self.state["docs"] if dt == "QAS PAYG Booking"]), 1)

    def test_exact_72_hours_and_retry(self):
        first = booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.assertIs(booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True), first)
        with self.assertRaisesRegex(ValueError, "another reservation"):
            booking.confirm_booking("S-1", "CS-OTHER", "A", "req-1", confirmed_rules=True)
        self.assertEqual(len([dt for dt, _ in self.state["docs"] if dt == "QAS PAYG Booking"]), 1)

    def test_attendance_failure_uses_savepoint(self):
        booking.session_resources.reserve_regular_place.side_effect = RuntimeError("attendance failed")
        with self.assertRaisesRegex(RuntimeError, "attendance failed"):
            booking.confirm_booking("S-1", "CS-1", "A", "req-1", confirmed_rules=True)
        self.db.rollback.assert_called_once()
        self.assertEqual(self.db.rollback.call_args.kwargs.keys(), {"save_point"})

    def test_cancel_boundary_and_preserve_attendance_row(self):
        reserved = self.add("QAS PAYG Booking", "PB-1", family_parent="P-1", student="S-1",
                            card="A", course_session="CS-1", status="Reserved", attendance_entry="ATT-1")
        self.add("Class Attendance Entry", "ATT-1", status="To be started", source_document="PB-1", marked_by="T-1")
        booking.session_resources.active_rows.return_value = [frappe._dict(name="ATT-1")]
        with self.assertRaisesRegex(ValueError, "72 hours"):
            booking.cancel_booking("PB-1")
        booking._now.return_value = datetime(2026, 9, 24, 9, 59)
        self.assertIs(booking.cancel_booking("PB-1"), reserved)
        self.assertIs(booking.cancel_booking("PB-1"), reserved)
        entries = [d for (dt, _), d in self.state["docs"].items() if dt == "QAS PAYG Entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].kind, entries[0].operation_key), ("Return", "return:PB-1"))
        self.assertEqual(self.state["docs"][("Class Attendance Entry", "ATT-1")].source_document, "PB-1")
        self.assertEqual(self.state["docs"][("Class Attendance Entry", "ATT-1")].marked_by, "T-1")
        self.db.set_value.assert_called_once_with("Class Attendance Entry", "ATT-1", "status", "Cancelled")


    def test_page_uses_cursor_and_reports_more_without_date_horizon(self):
        self.add("Course Sessions", "CS-2", weekly_timeslot="W-1", session_date="2026-10-04", status="Scheduled")
        original = self.sql
        def two_rows(query, params, **kwargs):
            if "ORDER BY s.session_date" in query:
                return [frappe._dict(name="CS-1", session_date="2026-09-27"),
                        frappe._dict(name="CS-2", session_date="2026-10-04")]
            return original(query, params, **kwargs)
        self.db.sql.side_effect = two_rows
        result = booking.available_sessions("S-1", "C-1", limit=1)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], ("2026-09-27", "CS-1"))
        self.assertEqual([item["name"] for item in result["items"]], ["CS-1"])

    def test_preview_checks_capacity_and_teacher(self):
        booking.session_resources.classroom_capacity.return_value = 0
        self.state["docs"][("Weekly Timeslot", "W-1")].teacher = None
        result = booking.preview_booking("S-1", "CS-1")
        self.assertFalse(result["bookable"])
        self.assertIn("full", result["reasons"])
        self.assertIn("teacher_missing", result["reasons"])

    def test_early_locked_booking_returns_consumed_unit(self):
        reserved = self.add("QAS PAYG Booking", "PB-1", family_parent="P-1", student="S-1",
                            card="A", course_session="CS-1", status="Locked", attendance_entry="ATT-1")
        self.add("Class Attendance Entry", "ATT-1", status="To be started", source_document="PB-1")
        booking.session_resources.active_rows.return_value = [frappe._dict(name="ATT-1")]
        self.db.exists.return_value = True
        booking._now.return_value = datetime(2026, 9, 24, 9, 59)
        booking.cancel_booking("PB-1")
        entry = next(doc for (dt, _), doc in self.state["docs"].items() if dt == "QAS PAYG Entry")
        self.assertEqual((entry.available_delta, entry.reserved_delta, entry.consumed_delta), (1, 0, -1))
        self.assertEqual(reserved.status, "Cancelled")

    def test_cursor_has_more_across_all_open_terms(self):
        booking.available_sessions("S-1", "C-1", cursor=("2027-04-01", "CS-0"), limit=1)
        sql = next(call for call in self.db.sql.call_args_list if "ORDER BY s.session_date" in call.args[0])
        self.assertIn("ORDER BY s.session_date, s.name LIMIT %s", sql.args[0])
        self.assertNotIn("60", sql.args[0])
        self.assertEqual(sql.args[1][-1], 2)
