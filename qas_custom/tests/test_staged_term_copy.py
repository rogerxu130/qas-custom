from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import (
	_copy_active_full_term_enrollments,
	_copy_term_weekly_timeslots,
)


class TestStagedTermCopy(TestCase):
	@patch("qas_custom.services.school_admin._set_if_field")
	@patch("qas_custom.services.school_admin.frappe.new_doc")
	@patch("qas_custom.services.school_admin._matching_target_timeslot", return_value=None)
	@patch("qas_custom.services.school_admin._safe_fields")
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_first_stage_copies_only_active_timeslots(self, get_all, safe_fields, matching, new_doc, set_field):
		safe_fields.return_value = ["name", "term", "course", "status"]
		get_all.return_value = [frappe._dict(name="WT-SOURCE", course="COURSE-1", status="Active")]
		new_doc.return_value = SimpleNamespace(name="WT-TARGET", insert=Mock())

		mapping, summary = _copy_term_weekly_timeslots("TERM-SOURCE", "TERM-TARGET", active_only=True)

		self.assertEqual(mapping, {"WT-SOURCE": "WT-TARGET"})
		self.assertEqual(summary, {"eligible": 1, "created": 1, "existing": 0})
		self.assertEqual(get_all.call_args.kwargs["filters"], {"term": "TERM-SOURCE", "status": "Active"})
		set_field.assert_any_call(new_doc.return_value, "copied_from_weekly_timeslot", "WT-SOURCE")

	@patch("qas_custom.services.school_admin._add_comment")
	@patch("qas_custom.services.school_admin._apply_enrollment_payload")
	@patch("qas_custom.services.school_admin.frappe.new_doc")
	@patch("qas_custom.services.school_admin._existing_target_enrollment")
	@patch("qas_custom.services.school_admin._source_term_enrollments_for_planning")
	@patch("qas_custom.services.school_admin.frappe.get_doc")
	def test_second_stage_creates_planned_enrollments_and_reports_unmapped_rows(
		self,
		get_doc,
		source_rows,
		existing,
		new_doc,
		apply_payload,
		_add_comment,
	):
		get_doc.return_value = {"start_date": "2026-10-01"}
		source_rows.return_value = [
			{"name": "ENR-1", "student": "STU-1", "parent": "PAR-1", "course": "COURSE-1", "weekly_timeslot": "WT-1"},
			{"name": "ENR-2", "student": "STU-2", "parent": "PAR-2", "course": "COURSE-2", "weekly_timeslot": "WT-MISSING"},
		]
		existing.return_value = None
		new_doc.return_value = SimpleNamespace(name="ENR-TARGET", insert=Mock())

		summary = _copy_active_full_term_enrollments("TERM-SOURCE", "TERM-TARGET", {"WT-1": "WT-TARGET"})

		self.assertEqual(summary["eligible"], 2)
		self.assertEqual(summary["created"], 1)
		self.assertEqual(summary["skipped"], 1)
		self.assertEqual(summary["skipped_rows"][0]["enrollment"], "ENR-2")
		self.assertEqual(apply_payload.call_args.args[1]["status"], "Planned")
		self.assertEqual(apply_payload.call_args.args[1]["weekly_timeslot"], "WT-TARGET")
