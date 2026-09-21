import copy
import json
from collections import Counter
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import term4_timetable_preview as service


class TestTerm4TimetablePreview(TestCase):
    def setUp(self):
        self.before = dict(course="Art", class_language="English", campus="Campus", classroom="room", day_of_week="Saturday", start_time="09:00:00", end_time="10:00:00", teacher="Teacher A", status="Active")
        self.target = {**self.before, "teacher": "Teacher B"}
        self.plan = {"term": service.SUPPORTED_TERM, "actions": [{"action": "update", "source": "WTS", "expected": self.before, "target": self.target}]}
        self.snapshot = {
            "term": dict(start_date="2026-10-05", end_date="2026-10-11", status="Upcoming"),
            "slots": [{"name": "WTS", **self.before}],
            "sessions": [dict(name="CS", weekly_timeslot="WTS", session_date="2026-10-10", status="Scheduled")],
            "references": [],
            "rooms": [dict(name="room", classroom_name="R1", campus="Campus", status="Active")],
            "courses": [dict(name="Art", duration_mins=60)],
            "teachers": [dict(name=t, status="Active") for t in ("Teacher A", "Teacher B")],
        }

    def review(self):
        return service._review(self.plan, self.snapshot, "2026-09-21")

    def test_update_preserves_session_ids_and_does_not_mutate_snapshot(self):
        original = copy.deepcopy(self.snapshot)
        result = self.review()
        self.assertEqual(result["blocking_errors"], [])
        self.assertEqual(result["actions"][0]["session_ids"], ["CS"])
        self.assertEqual(result["actions"][0]["changes"], {"teacher": {"before": "Teacher A", "after": "Teacher B"}})
        self.assertFalse(result["executable"])
        self.assertEqual(original, self.snapshot)

    def test_new_teacher_missing_blocks(self):
        self.target["teacher"] = "Missing"
        self.assertTrue(any("Teacher is missing" in s for s in self.review()["blocking_errors"]))

    def test_unassigned_teacher_is_valid(self):
        self.target["teacher"] = ""
        self.assertEqual(self.review()["blocking_errors"], [])

    def test_changed_live_class_blocks(self):
        self.snapshot["slots"][0]["start_time"] = "09:15:00"
        self.assertTrue(any("baseline" in s for s in self.review()["blocking_errors"]))

    def test_added_live_class_blocks(self):
        self.snapshot["slots"].append({"name": "NEW", **self.before})
        self.assertTrue(any("class set" in s for s in self.review()["blocking_errors"]))

    def test_deletion_blocked_by_trial_or_dynamic_reference(self):
        self.plan["actions"][0].update(action="delete_candidate", target=None)
        for dt in ("Inquiry", "Communication"):
            with self.subTest(doctype=dt):
                self.snapshot["references"] = [dict(doctype=dt, field="course_session", target="CS", record={"name": "REF"})]
                self.assertTrue(any("Deletion candidate has live references" in s for s in self.review()["blocking_errors"]))

    def test_empty_deletion_lists_exact_sessions(self):
        self.plan["actions"][0].update(action="delete_candidate", target=None)
        result = self.review()
        self.assertEqual(result["blocking_errors"], [])
        self.assertEqual(result["summary"]["sessions_to_remove"], 1)
        self.assertEqual(result["actions"][0]["session_ids"], ["CS"])

    def test_course_change_with_cancelled_enrollment_is_not_treated_as_empty(self):
        self.target["course"] = "New Art"
        self.snapshot["courses"].append(dict(name="New Art", duration_mins=60))
        self.snapshot["references"] = [dict(doctype="Enrollment", field="weekly_timeslot", target="WTS", record={"name": "ENR", "status": "Cancelled"})]
        self.assertTrue(any("Course change has enrollments" in s for s in self.review()["blocking_errors"]))

    def test_attendance_override_and_calendar_exceptions_block(self):
        self.snapshot["sessions"][0]["teacher_override"] = "Teacher A"
        self.snapshot["sessions"][0]["session_date"] = "2026-10-11"
        self.snapshot["references"] = [dict(doctype="Class Attendance Entry", field="course_session", target="CS", record={"name": "ATT", "status": "Present"})]
        errors = self.review()["blocking_errors"]
        self.assertTrue(any("marked" in e for e in errors))
        self.assertTrue(any("overrides" in e for e in errors))
        self.assertTrue(any("calendar" in e for e in errors))

    def test_new_class_dates_respect_term_boundaries(self):
        self.plan["actions"].append(dict(action="create_candidate", source=None, expected=None, target={**self.before, "day_of_week": "Monday"}))
        result = self.review()
        self.assertEqual(result["blocking_errors"], [])
        self.assertEqual(result["actions"][1]["new_session_dates"], ["2026-10-05"])

    def test_final_teacher_conflict_blocks(self):
        self.snapshot["rooms"].append(dict(name="other", classroom_name="R2", campus="Campus", status="Active"))
        self.plan["actions"].append(dict(action="create_candidate", source=None, expected=None, target={**self.target, "classroom": "other"}))
        self.assertTrue(any("overlap" in e for e in self.review()["blocking_errors"]))

    def test_token_changes_with_reference_status(self):
        before = self.review()["review_token"]
        self.snapshot["references"] = [dict(doctype="Enrollment", field="weekly_timeslot", target="WTS", record={"name": "ENR", "status": "Planned"})]
        after = self.review()["review_token"]
        self.assertNotEqual(before, after)
        self.snapshot["references"][0]["record"]["status"] = "Active"
        self.assertNotEqual(after, self.review()["review_token"])

    def test_preview_only_reads_database(self):
        db = Mock()
        db.get_value.return_value = self.snapshot["term"]
        groups = {"Weekly Timeslot": self.snapshot["slots"], "Course Sessions": self.snapshot["sessions"], "Classroom": self.snapshot["rooms"], "Teacher": self.snapshot["teachers"], "Course": self.snapshot["courses"]}
        def read(dt, filters, fields):
            return [frappe._dict(r) for r in groups[dt]]
        with patch.object(service, "_require_access"), patch.object(service.Path, "read_text", return_value=json.dumps(self.plan)), patch.object(service, "_rows", side_effect=read), patch.object(service, "_references", return_value=[]), patch.object(service, "today", return_value="2026-09-21"), patch.object(service.frappe, "get_meta", return_value=Mock()), patch.object(service.frappe, "db", new=db):
            self.assertTrue(service.preview()["read_only"])
        self.assertEqual([c[0] for c in db.mock_calls], ["get_value"])

    def test_reference_discovery_reads_custom_owner_and_dynamic_links(self):
        def get_all(dt, **kwargs):
            if dt == "DocField":
                self.assertEqual(kwargs["fields"], ["parent", "fieldname", "options"])
                return []
            self.assertEqual(kwargs["fields"], ["dt", "fieldname", "options"])
            return [frappe._dict(dt="Communication", fieldname="reference_name", options="reference_doctype")]
        meta = Mock(issingle=False)
        meta.has_field.return_value = False
        with patch.object(service, "_link_fields", return_value=[]), patch.object(service.frappe, "get_all", side_effect=get_all), patch.object(service.frappe, "get_meta", return_value=meta), patch.object(service, "_rows", return_value=[{"name": "REF", "reference_name": "CS"}]) as rows:
            result = service._references("Course Sessions", ["CS"])
        self.assertTrue(result[0]["dynamic"])
        self.assertEqual(rows.call_args.args[1], [["reference_doctype", "=", "Course Sessions"], ["reference_name", "in", ["CS"]]])

    def test_unreadable_references_block_and_other_checks_continue(self):
        meta = Mock(issingle=False)
        meta.has_field.return_value = False
        for failure in (None, RuntimeError("Query unavailable")):
            with self.subTest(failure=failure):
                errors = []
                with patch.object(service, "_link_fields", return_value=[("Unreadable", "reference"), ("Enrollment", "weekly_timeslot")]), patch.object(service.frappe, "get_all", return_value=[]), patch.object(service.frappe, "get_meta", return_value=meta), patch.object(service, "_rows", side_effect=[failure, [{"name": "ENR", "weekly_timeslot": "WTS"}]]):
                    refs = service._references("Weekly Timeslot", ["WTS"], errors)
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0]["doctype"], "Enrollment")
                self.assertEqual(len(errors), 1)
                self.assertIn("Unreadable.reference -> Weekly Timeslot", errors[0])
                self.snapshot["reference_read_errors"] = errors
                result = self.review()
                self.assertIn(errors[0], result["blocking_errors"])
                self.assertFalse(result["executable"])

    def test_none_dynamic_reference_result_reports_exact_field(self):
        meta = Mock(issingle=False)
        meta.has_field.return_value = False
        errors = []
        schema = [frappe._dict(parent="Virtual Record", fieldname="reference_name", options="reference_doctype")]
        with patch.object(service, "_link_fields", return_value=[]), patch.object(service.frappe, "get_all", side_effect=[schema, []]), patch.object(service.frappe, "get_meta", return_value=meta), patch.object(service, "_rows", return_value=None):
            self.assertEqual(service._references("Course Sessions", ["CS"], errors), [])
        self.assertIn("Virtual Record.reference_name -> Course Sessions", errors[0])
        self.assertIn("NoneType", errors[0])

    def test_reference_errors_cannot_be_silently_discarded(self):
        meta = Mock(issingle=False)
        meta.has_field.return_value = False
        with patch.object(service, "_link_fields", return_value=[("Unreadable", "reference")]), patch.object(service.frappe, "get_meta", return_value=meta), patch.object(service, "_rows", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Reference check incomplete"):
                service._references("Weekly Timeslot", ["WTS"])

    def test_bundled_plan_covers_every_source_once_and_preserves_user_choices(self):
        plan = json.loads(Path(service.__file__).with_name("term4_timetable_plan.json").read_text())
        actions = plan["actions"]
        sources = [r["source"] for r in actions if r["source"]]
        self.assertEqual(len(sources), 135)
        self.assertEqual(len(set(sources)), 135)
        self.assertEqual(Counter(r["action"] for r in actions), {"unchanged": 87, "update": 35, "create_candidate": 5, "delete_candidate": 12, "hold_referenced": 1})
        targets = [r["target"] for r in actions if r["target"]]
        empty = [r for r in targets if not r["teacher"]]
        self.assertEqual({r["start_time"] for r in empty}, {"09:00:00", "13:40:00"})
        for r in targets:
            if r["campus"] == "Upper Mount Gravatt" and r["day_of_week"] == "Saturday" and r["classroom"] == "upper-mount-gravatt-r1" and r["start_time"] in ("09:00:00", "13:00:00"):
                self.assertEqual(r["teacher"], "Jayson")
                self.assertIn(r["course"], ("Designer Art - Beginner", "Creative Art - Intermediate A"))
