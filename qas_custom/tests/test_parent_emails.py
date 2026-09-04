import csv
import io
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.parent_emails import (
	_build_term_parent_email_csv,
	_effective_teacher,
	_parent_recipient,
	_safe_csv_value,
	_session_recipients,
	_term_parent_email_export_filename,
	_validate_term_parent_email_export,
	export_school_admin_term_parent_emails_data,
	get_school_admin_term_parent_email_export_summary_data,
)


class TestParentEmailAudienceResolution(TestCase):
	def test_effective_teacher_prefers_dated_override_then_session_then_weekly_teacher(self):
		self.assertEqual(
			_effective_teacher(
				{"teacher_override": "TEACHER-OVERRIDE", "teacher": "TEACHER-SESSION"},
				{"teacher": "TEACHER-WEEKLY"},
			),
			"TEACHER-OVERRIDE",
		)
		self.assertEqual(
			_effective_teacher({"teacher": "TEACHER-SESSION"}, {"teacher": "TEACHER-WEEKLY"}),
			"TEACHER-SESSION",
		)
		self.assertEqual(_effective_teacher({}, {"teacher": "TEACHER-WEEKLY"}), "TEACHER-WEEKLY")

	@patch("qas_custom.services.parent_emails._parent_recipient")
	@patch("qas_custom.services.parent_emails._student_map")
	@patch("qas_custom.services.parent_emails.frappe.get_all")
	def test_session_recipients_use_actual_attendance_and_deduplicate_parent(self, get_all, student_map, parent_recipient):
		get_all.return_value = [
			frappe._dict(course_session="SESSION-1", student="STUDENT-1", enrollment_type="Full-Term"),
			frappe._dict(course_session="SESSION-1", student="STUDENT-2", enrollment_type="Trial"),
		]
		student_map.return_value = {
			"STUDENT-1": {"parent": "PARENT-1", "label": "Student One"},
			"STUDENT-2": {"parent": "PARENT-1", "label": "Student Two"},
		}
		parent_recipient.side_effect = lambda parent, **values: {"parent": parent, **values}

		result = _session_recipients(["SESSION-1"])

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["parent"], "PARENT-1")
		self.assertEqual(result[0]["students"], ["Student One", "Student Two"])
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["status"],
			["not in", ["Cancelled", "Leave"]],
		)

	def test_unsubscribed_parent_remains_visible_but_is_not_eligible(self):
		parent = frappe._dict(
			name="PARENT-1",
			parent_name="Parent One",
			email="parent@example.com",
			status="Active",
			mass_email_unsubscribed=1,
		)
		meta = SimpleNamespace(has_field=Mock(return_value=True))
		fake_frappe = SimpleNamespace(
			get_meta=Mock(return_value=meta),
			db=SimpleNamespace(get_value=Mock(return_value=parent)),
		)
		with patch("qas_custom.services.parent_emails.frappe", fake_frappe):
			result = _parent_recipient(
				"PARENT-1",
				students=["Student One"],
				student_ids=["STUDENT-1"],
				match_reasons=["Term 2026-T3"],
			)

		self.assertFalse(result["eligible"])
		self.assertFalse(result["selected"])
		self.assertEqual(result["reason"], "Parent unsubscribed from mass emails.")
		self.assertEqual(result["email"], "parent@example.com")


class TestTermParentEmailExport(TestCase):
	@patch("qas_custom.services.parent_emails._term_recipients")
	@patch("qas_custom.services.parent_emails._validate_term_parent_email_export", return_value="Term 3 2026")
	@patch("qas_custom.services.parent_emails._require_school_admin")
	def test_summary_counts_only_eligible_unique_parent_rows(self, require_admin, validate_term, term_recipients):
		term_recipients.return_value = [
			{"parent": "PARENT-1", "parent_name": "Parent One", "email": "one@example.com", "eligible": True},
			{"parent": "PARENT-2", "parent_name": "Parent Two", "email": "", "eligible": False},
		]

		result = get_school_admin_term_parent_email_export_summary_data("2026-T3")

		self.assertEqual(result, {"eligible_parent_count": 1})
		require_admin.assert_called_once_with()
		validate_term.assert_called_once_with("2026-T3")

	def test_csv_has_two_columns_utf8_bom_stable_order_and_formula_protection(self):
		rows = [
			{"parent": "PARENT-2", "parent_name": "张家长", "email": "zhang@example.com", "eligible": True},
			{"parent": "PARENT-1", "parent_name": "=Unsafe", "email": "one@example.com", "eligible": True},
		]

		content = _build_term_parent_email_csv(rows)
		decoded = content.decode("utf-8")
		parsed = list(csv.reader(io.StringIO(decoded.lstrip("\ufeff"))))

		self.assertTrue(decoded.startswith("\ufeff"))
		self.assertEqual(parsed[0], ["Parent Name", "Email"])
		self.assertEqual(parsed[1], ["'=Unsafe", "one@example.com"])
		self.assertEqual(parsed[2], ["张家长", "zhang@example.com"])
		self.assertEqual(_safe_csv_value("  +SUM(A1:A2)"), "'  +SUM(A1:A2)")

	def test_filename_is_safe_and_keeps_readable_term_name(self):
		self.assertEqual(
			_term_parent_email_export_filename("Term 3 / 2026"),
			"Term_3_2026_parent_emails.csv",
		)

	def test_unknown_term_is_rejected(self):
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(exists=Mock(return_value=False)),
			throw=Mock(side_effect=ValueError),
		)
		with (
			patch("qas_custom.services.parent_emails.frappe", fake_frappe),
			patch("qas_custom.services.parent_emails._", side_effect=lambda value: value),
			self.assertRaises(ValueError),
		):
			_validate_term_parent_email_export("UNKNOWN")

		fake_frappe.throw.assert_called_once_with("Term was not found.")

	@patch("qas_custom.services.parent_emails._term_recipients", return_value=[])
	@patch("qas_custom.services.parent_emails._validate_term_parent_email_export", return_value="Term 3 2026")
	@patch("qas_custom.services.parent_emails._require_school_admin")
	def test_download_rejects_empty_export(self, _require_admin, _validate_term, _term_recipients):
		fake_frappe = SimpleNamespace(
			throw=Mock(side_effect=ValueError),
			local=SimpleNamespace(response=SimpleNamespace()),
		)
		with (
			patch("qas_custom.services.parent_emails.frappe", fake_frappe),
			patch("qas_custom.services.parent_emails._", side_effect=lambda value: value),
			self.assertRaises(ValueError),
		):
			export_school_admin_term_parent_emails_data("2026-T3")

		fake_frappe.throw.assert_called_once_with("This Term has no eligible parent email addresses to export.")

	@patch("qas_custom.services.parent_emails._term_recipients")
	@patch("qas_custom.services.parent_emails._validate_term_parent_email_export", return_value="Term 3 2026")
	@patch("qas_custom.services.parent_emails._require_school_admin")
	def test_download_sets_csv_response(self, require_admin, validate_term, term_recipients):
		term_recipients.return_value = [
			{"parent": "PARENT-1", "parent_name": "Parent One", "email": "one@example.com", "eligible": True},
			{"parent": "PARENT-2", "parent_name": "Parent Two", "email": "two@example.com", "eligible": False},
		]
		fake_frappe = SimpleNamespace(local=SimpleNamespace(response=SimpleNamespace()))

		with patch("qas_custom.services.parent_emails.frappe", fake_frappe):
			result = export_school_admin_term_parent_emails_data("2026-T3")

		self.assertIsNone(result)
		self.assertEqual(fake_frappe.local.response.filename, "Term_3_2026_parent_emails.csv")
		self.assertEqual(fake_frappe.local.response.content_type, "text/csv; charset=utf-8")
		self.assertEqual(fake_frappe.local.response.type, "download")
		self.assertIn(b"one@example.com", fake_frappe.local.response.filecontent)
		self.assertNotIn(b"two@example.com", fake_frappe.local.response.filecontent)
