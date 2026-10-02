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


class TestDatedParentContactExport(TestCase):
	def test_date_requires_an_explicit_real_iso_date(self):
		from qas_custom.services.parent_contact_export import _validate_date
		with patch('qas_custom.services.parent_contact_export._', side_effect=lambda value: value), patch('frappe.throw', side_effect=ValueError):
			for value in (None, '', '02/10/2026', '2026-02-30', '2026-10-02 12:00:00'):
				with self.subTest(value=value), self.assertRaises(ValueError):
					_validate_date(value)
		self.assertEqual(str(_validate_date('2026-10-02')), '2026-10-02')

	def test_dated_roster_uses_actual_teacher_and_deduplicates_families(self):
		from contextlib import ExitStack
		from qas_custom.services import parent_contact_export as export
		from qas_custom.services import parent_emails as emails
		sessions = [
			dict(name='C1', weekly_timeslot='W1', teacher='T1', teacher_override='T2', course='Art'),
			dict(name='C2', weekly_timeslot='W2', teacher='T2', course='Art'),
			dict(name='C3', weekly_timeslot='W3', course='Art'),
		]
		with ExitStack() as stack:
			stack.enter_context(patch.object(emails, '_sessions_on_date', return_value=sessions))
			stack.enter_context(patch.object(emails, '_timeslot_map', return_value={'W1': {'teacher': 'T1'}, 'W2': {'teacher': 'T1'}, 'W3': {'teacher': 'T1'}}))
			stack.enter_context(patch.object(emails, '_teacher_map', return_value={'T1': 'One', 'T2': 'Two'}))
			stack.enter_context(patch.object(export.frappe, 'db', Mock(exists=Mock(return_value=True))))
			query = stack.enter_context(patch.object(export.frappe, 'get_all', return_value=[dict(student='S1', course_session='C1', enrollment_type='Trial'), dict(student='S1', course_session='C2', enrollment_type='Makeup'), dict(student='S2', course_session='C2', enrollment_type='Full-Term')]))
			stack.enter_context(patch.object(export, '_student_map', return_value={'S1': {'parent': 'P1', 'label': 'Alice'}, 'S2': {'parent': 'P2', 'label': 'Bob'}}))
			stack.enter_context(patch.object(export, '_parent_contact', side_effect=lambda parent: dict(parent_name=parent, email=' Shared@Example.com ', sms_number='0400')))
			rows, label = export._dated_rows('2026-10-02', 'T2')
			self.assertEqual(query.call_args.kwargs['filters']['course_session'], ['in', ['C1', 'C2']])
			self.assertEqual(query.call_args.kwargs['filters']['status'], ['not in', ['Cancelled', 'Leave']])
			self.assertEqual(query.call_args.kwargs['limit'], 0)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0]['email'], 'shared@example.com')
			self.assertIn('Alice', rows[0]['participant_name']); self.assertIn('Bob', rows[0]['participant_name'])
			self.assertIn('C1', rows[0]['source_label']); self.assertIn('C2', rows[0]['source_label'])
			self.assertIn('Two', label)
			export._dated_rows('2026-10-02')
			self.assertEqual(query.call_args.kwargs['filters']['course_session'], ['in', ['C1', 'C2', 'C3']])
			export._dated_rows('2026-10-02', 'T1')
			self.assertEqual(query.call_args.kwargs['filters']['course_session'], ['in', ['C3']])

	def test_empty_day_never_falls_back_to_term_roster(self):
		from qas_custom.services import parent_contact_export as export
		with patch('qas_custom.services.parent_emails._sessions_on_date', return_value=[]), patch('qas_custom.services.parent_emails._timeslot_map', return_value={}), patch.object(export.frappe, 'get_all') as query:
			rows, _ = export._dated_rows('2026-10-02')
			self.assertEqual(rows, [])
			query.assert_not_called()

	def test_date_export_uses_same_resolver_for_summary_and_csv(self):
		from qas_custom.services import parent_contact_export as export
		with patch.object(export, '_require_school_admin'), patch.object(export, '_dated_rows', return_value=([{'email': 'a@example.com'}], '2026-10-02')) as dated:
			result = export.get_school_admin_parent_contact_export_summary_data('date', session_date='2026-10-02', teacher='T')
			self.assertEqual(result['participant_count'], 1)
			self.assertEqual(str(dated.call_args.args[0]), '2026-10-02')
			self.assertEqual(dated.call_args.args[1], 'T')

	def test_permission_rejected_before_reading_roster(self):
		from qas_custom.services import parent_contact_export as export
		with patch.object(export, '_require_school_admin', side_effect=PermissionError), patch.object(export, '_resolve_export') as resolve:
			with self.assertRaises(PermissionError):
				export.get_school_admin_parent_contact_export_summary_data('date', session_date='2026-10-02')
			resolve.assert_not_called()

	def test_date_teacher_options_use_override_and_no_active_teacher_filter(self):
		from qas_custom.services import parent_contact_export as export
		with patch.object(export, '_require_school_admin'), patch('qas_custom.services.parent_emails._sessions_on_date', return_value=[{'name': 'C', 'teacher': 'T1', 'teacher_override': 'T2'}]), patch('qas_custom.services.parent_emails._timeslot_map', return_value={}), patch('qas_custom.services.parent_emails._teacher_map', return_value={'T2': 'Substitute'}) as teachers:
			result = export.get_school_admin_parent_contact_export_options_data('date', session_date='2026-10-02')
			teachers.assert_called_once_with({'T2'})
			self.assertEqual(result['items'], [{'name': 'T2', 'label': 'Substitute'}])

	def test_csv_endpoint_uses_selected_date_and_teacher(self):
		from qas_custom.services import parent_contact_export as export
		with patch.object(export, '_require_school_admin'), patch.object(export, '_dated_rows', return_value=([{'email': 'parent@example.com'}], '2026-10-02 Substitute')) as dated, patch.object(export.frappe, 'local', SimpleNamespace(response=SimpleNamespace())):
			export.export_school_admin_parent_contacts_data('date', session_date='2026-10-02', teacher='T2')
			self.assertEqual(str(dated.call_args.args[0]), '2026-10-02')
			self.assertEqual(dated.call_args.args[1], 'T2')
			self.assertIn('2026-10-02', export.frappe.local.response.filename)
			self.assertIn(b'parent@example.com', export.frappe.local.response.filecontent)
