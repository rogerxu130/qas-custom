"""Student activity is a result of participation, not permission to participate."""
import sqlite3
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import school_admin, maintenance
from qas_custom.qas_custom.doctype.enrollment.enrollment import Enrollment


class TestStudentActivityActions(TestCase):
    def test_manual_add_activates_only_after_success(self):
        events = []
        fake = SimpleNamespace(db=SimpleNamespace(exists=Mock(return_value=True),
            set_value=Mock(side_effect=lambda *args: events.append("activated")), commit=Mock()),
            get_doc=Mock(return_value={"status": "Scheduled"}))
        with patch.object(school_admin, "frappe", fake), \
                patch.object(school_admin, "_require_school_admin"), \
                patch.object(school_admin, "_get_payload", side_effect=lambda p: p), \
                patch.object(school_admin, "_has_field", return_value=True), \
                patch.object(school_admin, "_validate_course_session_attendance_type"), \
                patch.object(school_admin, "_validate_course_session_attendance_status"), \
                patch.object(school_admin, "_add_comment"), \
                patch.object(school_admin, "get_school_admin_course_session_data", return_value={}), \
                patch.object(school_admin, "create_attendance_entry", side_effect=lambda **kw: events.append("attendance") or "ATT") as create:
            school_admin.create_school_admin_course_session_attendance_data("CS", {"student": "S"})
            self.assertEqual(events, ["attendance", "activated"])
            fake.db.set_value.assert_called_once_with("Student", "S", "status", "Active")
            events.clear(); fake.db.set_value.reset_mock()
            create.side_effect = RuntimeError("Class full")
            with self.assertRaisesRegex(RuntimeError, "Class full"):
                school_admin.create_school_admin_course_session_attendance_data("CS", {"student": "S"})
            fake.db.set_value.assert_not_called()

    def test_open_enrollment_activates_student_but_closed_enrollment_does_not(self):
        from qas_custom.qas_custom.doctype.enrollment import enrollment
        update = Mock()
        fake = SimpleNamespace(get_meta=lambda dt: SimpleNamespace(has_field=lambda f: True), db=SimpleNamespace(set_value=update))
        with patch.object(enrollment, "frappe", fake):
            for status in ("Active", "Planned"):
                Enrollment.on_update(SimpleNamespace(student="S", status=status))
            self.assertEqual(update.call_count, 2)
            update.reset_mock()
            for status in ("Inactive", "Cancelled", "Completed"):
                Enrollment.on_update(SimpleNamespace(student="S", status=status))
            update.assert_not_called()

    def test_nightly_activity_keeps_valid_pass_users_not_unbooked_siblings(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE `tabStudent` (name TEXT, guardian TEXT);
            CREATE TABLE `tabQAS PAYG Card` (name TEXT, family_parent TEXT, status TEXT,
                issued_on TEXT, expires_on TEXT, available_count INTEGER, reserved_count INTEGER);
            CREATE TABLE `tabQAS PAYG Booking` (student TEXT, card TEXT, family_parent TEXT, status TEXT);
        """)
        students = [("USER", "P"), ("SIBLING", "P"), ("EXPIRED", "P"), ("CANCELLED", "P"),
                    ("EMPTY", "P"), ("MOVED", "OTHER"), ("RESERVED", "P")]
        db.executemany("INSERT INTO `tabStudent` VALUES (?,?)", students)
        cards = [("VALID", "P", "Active", "2026-09-01", "2027-03-01", 9, 0),
                 ("OLD", "P", "Active", "2026-01-01", "2026-07-01", 9, 0),
                 ("EMPTY", "P", "Active", "2026-09-01", "2027-03-01", 0, 0),
                 ("RESERVED", "P", "Active", "2026-09-01", "2027-03-01", 0, 1)]
        db.executemany("INSERT INTO `tabQAS PAYG Card` VALUES (?,?,?,?,?,?,?)", cards)
        db.executemany("INSERT INTO `tabQAS PAYG Booking` VALUES (?,?,?,?)", [
            ("USER", "VALID", "P", "Consumed"), ("EXPIRED", "OLD", "P", "Consumed"),
            ("CANCELLED", "VALID", "P", "Cancelled"), ("EMPTY", "EMPTY", "P", "Consumed"),
            ("MOVED", "VALID", "P", "Consumed"), ("RESERVED", "RESERVED", "P", "Reserved")])
        def sql(query, params, **kwargs):
            return [row[0] for row in db.execute(query.replace("%s", "?"), params)]
        with patch.object(maintenance, "_doctype_available", return_value=True), \
                patch.object(maintenance, "today", return_value="2026-10-05"), \
                patch.object(maintenance, "frappe", SimpleNamespace(db=SimpleNamespace(sql=sql))):
            self.assertEqual(maintenance._get_students_with_active_payg_cards(), {"USER", "RESERVED"})
