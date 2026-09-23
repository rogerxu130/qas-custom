"""The shared regular-session write boundary, without a site database."""

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.modules.course_schedule import session_resources as subject
from qas_custom.services import adhoc_booking, class_attendance


class TestRegularReservation(TestCase):
    def setUp(self):
        self.session = frappe._dict(name="CS-1", weekly_timeslot="WTS-1", session_date="2026-10-03", status="Scheduled")
        self.slot = frappe._dict(name="WTS-1", term="TERM-1", classroom="ROOM-1", teacher="TEA-1", status="Active", day_of_week="Saturday", start_time="10:00", end_time="11:00")
        self.term = frappe._dict(name="TERM-1", status="Active", start_date="2026-10-01", end_date="2026-12-31")
        self.events = []
        self.enrollment_rows = []
        docs = {("Course Sessions", "CS-1"): self.session, ("Weekly Timeslot", "WTS-1"): self.slot, ("Term", "TERM-1"): self.term}
        self.fake = SimpleNamespace(
            db=SimpleNamespace(sql=Mock(side_effect=self.sql), get_value=Mock(return_value=1)),
            get_doc=Mock(side_effect=lambda dt, name, **kw: self.lock_doc(docs, dt, name)),
            get_all=Mock(return_value=[]),
            throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(ValueError(str(message))),
        )
        self.patches = [patch.object(subject, "frappe", self.fake), patch.object(subject, "now_datetime", return_value=datetime(2026, 9, 23)), patch.object(subject, "active_rows", return_value=[]), patch.object(subject, "student_has_conflict", return_value=False), patch.object(subject, "classroom_capacity", side_effect=self.capacity), patch.object(subject, "create_attendance_entry", return_value="ATT-NEW")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def sql(self, query, params, **kwargs):
        if "FOR UPDATE" in query and "tabStudent" in query:
            self.events.append("Student:" + params[0])
        if "tabEnrollment" in query:
            self.events.append("Enrollment:" + params[0])
            return self.enrollment_rows
        return []

    def lock_doc(self, docs, dt, name):
        self.events.append(dt + ":" + name)
        return docs[(dt, name)]

    def capacity(self, slot, lock=False):
        self.events.append("Classroom:" + slot.classroom)
        return 1

    def reserve(self, student="STU-1", source="PB-1"):
        return subject.reserve_regular_place(student, "CS-1", "QAS PAYG Booking", source, "Pay-as-you-go")

    def test_lock_order_and_last_seat(self):
        subject.active_rows.side_effect = lambda *_args, **_kwargs: self.events.append("active_rows") or []
        subject.create_attendance_entry.side_effect = lambda **_kwargs: self.events.append("insert") or "ATT-NEW"
        callback = lambda *_args: self.events.append("business_validation")
        self.assertEqual(subject.reserve_regular_place("STU-1", "CS-1", "QAS PAYG Booking", "PB-1", "Pay-as-you-go", validate_business=callback), "ATT-NEW")
        self.assertEqual(self.events, ["Student:STU-1", "Course Sessions:CS-1", "Weekly Timeslot:WTS-1", "Term:TERM-1", "Classroom:ROOM-1", "active_rows", "Enrollment:WTS-1", "business_validation", "insert"])
        self.fake.get_doc.assert_has_calls([
            call("Course Sessions", "CS-1", for_update=True),
            call("Weekly Timeslot", "WTS-1", for_update=True),
            call("Term", "TERM-1", for_update=True),
        ])
        subject.classroom_capacity.assert_called_once_with(self.slot, lock=True)
        subject.active_rows.assert_called_once_with("CS-1", lock=True)
        subject.student_has_conflict.assert_called_once_with("STU-1", self.session, self.slot, lock=True)
        enrollment_query = next(item for item in self.fake.db.sql.call_args_list if "tabEnrollment" in item.args[0])
        self.assertIn("FOR UPDATE", enrollment_query.args[0])
        self.assertIn("status IN ('Planned', 'Active')", enrollment_query.args[0])
        self.assertEqual(enrollment_query.args[1], ("WTS-1",))
        self.assertEqual(enrollment_query.kwargs, {"as_dict": True})
        self.fake.get_all.assert_not_called()
        with patch.object(subject, "active_rows", return_value=[frappe._dict(name="ATT-1", student="STU-1")]):
            with self.assertRaisesRegex(ValueError, "full"):
                self.reserve("STU-2", "PB-2")

    def test_cancelled_row_does_not_block_new_source(self):
        self.assertEqual(self.reserve(source="PB-NEW"), "ATT-NEW")
        self.assertEqual(subject.create_attendance_entry.call_args.kwargs["prevent_student_duplicate"], False)
        self.assertEqual(subject.create_attendance_entry.call_args.kwargs["active_only_source"], True)
        self.assertEqual(subject.create_attendance_entry.call_args.kwargs["source_document"], "PB-NEW")

    def test_active_duplicate_and_overlap_block(self):
        with patch.object(subject, "active_rows", return_value=[frappe._dict(name="ATT-1", student="STU-1")]):
            with self.assertRaisesRegex(ValueError, "already"):
                self.reserve()
        with patch.object(subject, "student_has_conflict", return_value=True):
            with self.assertRaisesRegex(ValueError, "already"):
                self.reserve()

    def test_same_active_source_is_idempotent(self):
        with patch.object(subject, "active_rows", return_value=[frappe._dict(name="ATT-1", student="STU-1", source_doctype="QAS PAYG Booking", source_document="PB-1")]):
            self.assertEqual(self.reserve(), "ATT-1")
            subject.create_attendance_entry.assert_not_called()

    def test_missing_source_never_makes_an_existing_row_idempotent(self):
        with patch.object(subject, "active_rows", return_value=[frappe._dict(name="ATT-1", student="STU-1")]):
            with self.assertRaisesRegex(ValueError, "already"):
                subject.reserve_regular_place("STU-1", "CS-1", None, None, "Pay-as-you-go")

    def test_business_guard_rejects_before_attendance_insert(self):
        with patch.object(adhoc_booking, "now_datetime", return_value=datetime(2026, 10, 2)), \
             patch.object(adhoc_booking.frappe, "throw", side_effect=ValueError):
            with self.assertRaises(ValueError):
                subject.reserve_regular_place("STU-1", "CS-1", "Adhoc Booking", "AB-1", "Pay-as-you-go",
                                              validate_business=lambda session, slot, _term, _rows: adhoc_booking._validate_locked_adhoc_context(session, slot))
        subject.create_attendance_entry.assert_not_called()

    def test_cancelled_history_can_be_rebooked_through_boundary(self):
        old = frappe._dict(name="ATT-OLD", status="Cancelled", source_document="PB-OLD", marked_by="TEA-1")
        new = Mock()
        new.name = "ATT-NEW"
        new.meta.has_field.return_value = True
        queries = []

        def get_value(_doctype, filters, _field):
            queries.append(filters)
            return None if filters.get("status") == ["not in", ["Cancelled", "Leave"]] else old.name

        attendance_frappe = SimpleNamespace(db=SimpleNamespace(get_value=Mock(side_effect=get_value)),
                                            new_doc=Mock(return_value=new), get_doc=Mock(return_value=old))
        subject.create_attendance_entry.side_effect = class_attendance.create_attendance_entry
        with patch.object(class_attendance, "frappe", attendance_frappe):
            self.assertEqual(self.reserve(source="PB-NEW"), "ATT-NEW")
        subject.active_rows.assert_called_once_with("CS-1", lock=True)
        self.assertEqual(new.source_document, "PB-NEW")
        self.assertEqual(old.source_document, "PB-OLD")
        self.assertEqual(old.marked_by, "TEA-1")
        attendance_frappe.get_doc.assert_not_called()
        new.insert.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(len(queries), 2)

    def test_zero_capacity_closed_term_missing_teacher(self):
        with patch.object(subject, "classroom_capacity", return_value=0):
            with self.assertRaisesRegex(ValueError, "capacity"):
                self.reserve()
        self.term.status = "Archived"
        with self.assertRaisesRegex(ValueError, "term"):
            self.reserve()
        self.term.status = "Active"
        self.slot.teacher = None
        with self.assertRaisesRegex(ValueError, "teacher"):
            self.reserve()

    def test_teacher_override_and_ndis_limit(self):
        self.slot.teacher = None
        self.session.teacher_override = "TEA-SUB"
        self.slot.ndis_friendly = 1
        with patch.object(subject, "classroom_capacity", return_value=20), \
             patch("qas_custom.services.ndis_friendly.NDIS_FRIENDLY_CAPACITY", 2), \
             patch.object(subject, "active_rows", return_value=[frappe._dict(student="A"), frappe._dict(student="B")]):
            with self.assertRaisesRegex(ValueError, "full"):
                self.reserve()
        self.assertEqual(self.reserve(), "ATT-NEW")

    def test_planned_full_term_enrollment_holds_a_seat(self):
        self.enrollment_rows = [frappe._dict(student="STU-2", enrollment_type="Full-Term")]
        with self.assertRaisesRegex(ValueError, "full"):
            self.reserve()

    def test_non_full_term_enrollment_does_not_hold_capacity_but_blocks_same_student(self):
        self.enrollment_rows = [frappe._dict(student="STU-2", enrollment_type="Trial")]
        self.assertEqual(self.reserve(), "ATT-NEW")
        self.enrollment_rows = [frappe._dict(student="STU-1", enrollment_type="Trial")]
        with self.assertRaisesRegex(ValueError, "already has an enrollment"):
            self.reserve()

    def test_waited_student_lock_sees_new_enrollment_on_locking_read(self):
        original_sql = self.fake.db.sql.side_effect

        def waited_sql(query, params, **kwargs):
            if "tabStudent" in query:
                self.enrollment_rows = [frappe._dict(student="STU-2", enrollment_type="Full-Term")]
            return original_sql(query, params, **kwargs)

        self.fake.db.sql.side_effect = waited_sql
        with self.assertRaisesRegex(ValueError, "full"):
            self.reserve()
        subject.create_attendance_entry.assert_not_called()

    def test_waited_student_lock_sees_new_conflict_on_locking_read(self):
        def conflict(_student, _session, _slot, *, lock):
            self.assertTrue(lock)
            self.assertIn("Student:STU-1", self.events)
            return True

        subject.student_has_conflict.side_effect = conflict
        with self.assertRaisesRegex(ValueError, "already has a class"):
            self.reserve()
        subject.create_attendance_entry.assert_not_called()


class TestCancelledSourceHistory(TestCase):
    def test_cancelled_row_is_not_reused_or_modified(self):
        old = frappe._dict(name="ATT-OLD", status="Cancelled", source_document="PB-OLD", marked_by="Teacher")
        new = Mock(name="attendance")
        new.name = "ATT-NEW"
        new.meta.has_field.return_value = True
        filters = []

        def get_value(_doctype, query, _field):
            filters.append(query)
            if query.get("status") == ["not in", ["Cancelled", "Leave"]]:
                return None
            return old.name

        fake = SimpleNamespace(db=SimpleNamespace(get_value=Mock(side_effect=get_value)), new_doc=Mock(return_value=new), get_doc=Mock(return_value=old))
        with patch.object(class_attendance, "frappe", fake):
            result = class_attendance.create_attendance_entry(
                "CS-1", "STU-1", "Pay-as-you-go", "QAS PAYG Booking", "PB-NEW",
                prevent_student_duplicate=False, active_only_source=True,
            )
        self.assertEqual(result, "ATT-NEW")
        self.assertEqual(new.source_document, "PB-NEW")
        self.assertEqual(old.source_document, "PB-OLD")
        self.assertEqual(old.marked_by, "Teacher")
        new.insert.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(len(filters), 2)
