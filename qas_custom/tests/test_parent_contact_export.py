from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.parent_contact_export import (
	_build_csv,
	_sort_participation_types,
	_weekly_timeslot_rows,
	_workshop_rows,
	get_school_admin_parent_contact_export_summary_data,
)


class TestParentContactExport(TestCase):
	def test_csv_has_expected_columns_bom_and_formula_protection(self):
		content = _build_csv([
			{
				"participant_name": "=Student",
				"parent_name": "Parent One",
				"email": "parent@example.com",
				"sms_number": "0400000000",
				"participation_type": "Trial",
				"source_label": "Saturday Art",
				"campus": "Indooroopilly",
			}
		])

		self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
		self.assertIn(b"Student / Participant Name,Parent Name,Email,SMS Number", content)
		self.assertIn(b"'=Student", content)

	def test_participation_types_use_stable_business_order(self):
		self.assertEqual(
			_sort_participation_types(["Makeup", "Trial", "Full-Term", "Trial"]),
			["Full-Term", "Trial", "Makeup"],
		)

	@patch("qas_custom.services.parent_contact_export._require_school_admin")
	@patch("qas_custom.services.parent_contact_export._resolve_export")
	def test_summary_counts_missing_contacts_without_dropping_rows(self, resolve_export, _require_admin):
		resolve_export.return_value = ([
			{"email": "one@example.com", "sms_number": "0400"},
			{"email": "", "sms_number": "0401"},
			{"email": "two@example.com", "sms_number": ""},
		], "Class")

		result = get_school_admin_parent_contact_export_summary_data("weekly_timeslot", "WTS-1", "TERM-1")

		self.assertEqual(result["participant_count"], 3)
		self.assertEqual(result["missing_email_count"], 1)
		self.assertEqual(result["missing_sms_count"], 1)

	@patch("qas_custom.services.parent_contact_export._parent_contact", return_value={"parent_name": "Parent", "email": "parent@example.com", "sms_number": "0400"})
	@patch("qas_custom.services.parent_contact_export._student_map")
	@patch("qas_custom.services.parent_contact_export.get_datetime_in_timezone", return_value=datetime(2026, 9, 21, 9, 0))
	def test_specific_class_includes_regular_and_future_temporary_students_once(self, _now, student_map, _contact):
		get_value = Mock()
		get_all = Mock()
		get_value.side_effect = [
			frappe._dict(name="WTS-1", term="TERM-1", course="Creative Art", campus="West End", day_of_week="Saturday", start_time="10:00:00"),
			"2026-12-01",
		]
		get_all.side_effect = [
			[frappe._dict(student="STU-1", parent="PAR-1")],
			[frappe._dict(name="SESSION-1")],
			[
				frappe._dict(student="STU-1", enrollment_type="Makeup"),
				frappe._dict(student="STU-2", enrollment_type="Trial"),
				frappe._dict(student="STU-2", enrollment_type="Trial"),
			],
		]
		student_map.return_value = {
			"STU-1": {"parent": "PAR-1", "label": "Student One"},
			"STU-2": {"parent": "PAR-2", "label": "Student Two"},
		}

		fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=get_value), get_all=get_all)
		with (
			patch("qas_custom.services.parent_contact_export.frappe", fake_frappe),
			patch("qas_custom.services.parent_contact_export.has_field", return_value=True),
		):
			rows, _label = _weekly_timeslot_rows("WTS-1", "TERM-1")

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["participation_type"], "Full-Term, Makeup")
		self.assertEqual(rows[1]["participation_type"], "Trial")
		attendance_filters = get_all.call_args_list[2].kwargs["filters"]
		self.assertEqual(attendance_filters["status"], ["not in", ["Cancelled", "Leave"]])

	@patch("qas_custom.services.parent_contact_export._parent_contact", return_value={"parent_name": "Parent", "email": "parent@example.com", "sms_number": "0400"})
	@patch("qas_custom.services.parent_contact_export._student_map", return_value={"STU-1": {"parent": "PAR-1", "label": "Student One"}})
	def test_workshop_uses_valid_enrollments_and_deduplicates_student(self, _student_map, _contact):
		get_value = Mock()
		get_all = Mock()
		get_value.return_value = frappe._dict(name="WSO-1", title="Spring Painting", campus="West End")
		get_all.return_value = [
			frappe._dict(name="WEN-1", student="STU-1", parent="PAR-1"),
			frappe._dict(name="WEN-2", student="STU-1", parent="PAR-1"),
			frappe._dict(name="WEN-3", student="STU-ADULT", parent="PAR-2", adult_participant_name="Adult Artist"),
		]

		fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=get_value), get_all=get_all)
		with (
			patch("qas_custom.services.parent_contact_export.frappe", fake_frappe),
			patch("qas_custom.services.parent_contact_export.has_field", return_value=True),
		):
			rows, label = _workshop_rows("WSO-1")

		self.assertEqual(label, "Spring Painting")
		self.assertEqual(len(rows), 2)
		self.assertEqual({row["participant_name"] for row in rows}, {"Student One", "Adult Artist"})
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["status"],
			["in", ["Planned", "Active", "Completed"]],
		)
