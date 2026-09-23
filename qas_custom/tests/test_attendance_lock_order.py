"""Shared attendance command serializes marks with PAYG settlement."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.attendance import commands
from qas_custom.services import teacher_portal


class Row(frappe._dict):
    def __init__(self, events, **values):
        super().__init__(values)
        self.events = events

    def save(self, **_kwargs):
        self.events.append("save")


class TestAttendanceLockOrder(TestCase):
    def setUp(self):
        self.events = []
        self.row = Row(self.events, name="ATT-1", student="S-1", course_session="CS-1",
                       status="Leave", comments="", source_doctype="QAS PAYG Booking",
                       source_document="PB-1")
        def get_value(_doctype, _name, _fields, **_kwargs):
            self.events.append("read-identifiers")
            return frappe._dict(student="S-1", course_session="CS-1")
        def sql(query, params):
            self.events.append("Student:" + params[0])
            self.assertIn("FOR UPDATE", query)
            return [(params[0],)]
        def get_doc(_doctype, _name, **kwargs):
            self.events.append("attendance-current")
            self.assertTrue(kwargs.get("for_update"))
            return self.row
        self.fake = SimpleNamespace(
            db=SimpleNamespace(get_value=Mock(side_effect=get_value), sql=Mock(side_effect=sql)),
            get_doc=Mock(side_effect=get_doc),
            session=SimpleNamespace(user="teacher@example.com"),
            throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
        )
        self.patches = [patch.object(commands, "frappe", self.fake),
                        patch.object(commands, "_validate_status"),
                        patch.object(commands, "now_datetime", return_value="2026-09-24 10:00:00"),
                        patch.object(commands, "set_if_field", side_effect=lambda row, field, value: setattr(row, field, value)),
                        patch.object(commands, "sync_trial_inquiry_status_from_attendance")]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def test_student_lock_precedes_current_attendance_read_and_save(self):
        def access(**kwargs):
            self.events.append("access")
            self.assertIs(kwargs["row"], self.row)
        result = commands.update_attendance_status("CS-1", "ATT-1", "Present", validate_access=access)
        self.assertTrue(result["changed"])
        self.assertEqual(self.events[:5], ["read-identifiers", "Student:S-1",
                                          "attendance-current", "access", "save"])

    def test_rejects_student_and_session_changed_while_waiting(self):
        for field, value in (("student", "S-2"), ("course_session", "CS-2")):
            original = self.row[field]
            self.row[field] = value
            with self.assertRaisesRegex(ValueError, "Attendance.*changed|Invalid attendance"):
                commands.update_attendance_status("CS-1", "ATT-1", "Present")
            self.row[field] = original
        self.assertNotIn("save", self.events)

    def test_expected_student_mismatch_is_rejected_before_lock(self):
        with self.assertRaisesRegex(ValueError, "Attendance student changed"):
            commands.update_attendance_status(
                "CS-1", "ATT-1", "Present", expected_student="S-2",
            )
        self.assertEqual(self.events, ["read-identifiers"])

    def test_teacher_guard_sees_current_leave_and_cancelled_after_student_lock(self):
        with patch.object(teacher_portal, "frappe", self.fake):
            for blocked in ("Leave", "Cancelled"):
                self.row.status = blocked
                def access(**kwargs):
                    self.events.append("teacher-access")
                    teacher_portal._is_blocked_teacher_attendance_update(
                        "CS-1", "ATT-1", {"status": "Present", "comments": ""},
                        current=kwargs["row"],
                    )
                with self.assertRaisesRegex(ValueError, "Teachers cannot change"):
                    commands.update_attendance_status(
                        "CS-1", "ATT-1", "Present", validate_access=access,
                    )
        self.assertNotIn("save", self.events)
        self.assertLess(self.events.index("Student:S-1"), self.events.index("teacher-access"))


class TestTeacherBatchLockOrder(TestCase):
    def setUp(self):
        mapping = {"ATT-A": ("S-2", "CS-1"), "ATT-B": ("S-1", "CS-1")}
        def get_value(_doctype, row_id, _fields, **_kwargs):
            student, session = mapping[row_id]
            return frappe._dict(student=student, course_session=session)
        self.fake = SimpleNamespace(
            session=SimpleNamespace(user="teacher@example.com"),
            db=SimpleNamespace(get_value=Mock(side_effect=get_value), commit=Mock()),
            throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
        )
        self.calls = []
        def mark(**kwargs):
            self.calls.append((kwargs["expected_student"], kwargs["attendance_row"]))
        self.patches = [patch.object(teacher_portal, "frappe", self.fake),
                        patch.object(teacher_portal, "reject_support_view_write"),
                        patch.object(teacher_portal, "_require_teacher", return_value=SimpleNamespace(name="T-1")),
                        patch.object(teacher_portal, "_get_request_json", return_value={}),
                        patch.object(teacher_portal, "_get_owned_session", return_value={"name": "CS-1"}),
                        patch.object(teacher_portal, "_parse_attendance_updates", side_effect=lambda updates: updates),
                        patch.object(teacher_portal, "update_attendance_status", side_effect=mark),
                        patch.object(teacher_portal, "get_teacher_session_detail_data", return_value={})]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def test_reverse_payloads_acquire_same_student_order(self):
        a = {"row_id": "ATT-A", "status": "Present"}
        b = {"row_id": "ATT-B", "status": "Present"}
        for updates in ([a, b], [b, a]):
            self.calls.clear()
            teacher_portal.update_teacher_attendance_data("CS-1", updates)
            self.assertEqual(self.calls, [("S-1", "ATT-B"), ("S-2", "ATT-A")])

    def test_duplicate_row_rejected_before_any_write(self):
        updates = [{"row_id": "ATT-A", "status": "Present"},
                   {"row_id": "ATT-A", "status": "Absent"}]
        with self.assertRaisesRegex(ValueError, "Duplicate attendance row"):
            teacher_portal.update_teacher_attendance_data("CS-1", updates)
        self.assertEqual(self.calls, [])

    def test_teacher_endpoint_checks_blocked_status_from_locked_row(self):
        def invoke_guard(**kwargs):
            kwargs["validate_access"](
                course_session="CS-1", attendance_row="ATT-A",
                row=frappe._dict(status="Leave", comments=""),
            )
        teacher_portal.update_attendance_status.side_effect = invoke_guard
        with self.assertRaisesRegex(ValueError, "Teachers cannot change"):
            teacher_portal.update_teacher_attendance_data(
                "CS-1", [{"row_id": "ATT-A", "status": "Present"}],
            )
        self.fake.db.commit.assert_not_called()

    def test_teacher_batch_rejects_other_session_before_writes(self):
        self.fake.db.get_value.side_effect = lambda _dt, _row, _fields, **_kwargs: frappe._dict(
            student="S-1", course_session="CS-OTHER",
        )
        with self.assertRaisesRegex(ValueError, "Invalid attendance row"):
            teacher_portal.update_teacher_attendance_data(
                "CS-1", [{"row_id": "ATT-A", "status": "Present"}],
            )
        self.assertEqual(self.calls, [])
