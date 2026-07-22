from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.announcements import (
	_announcement_student_preview,
	_resolve_announcement_recipients,
	_student_search_rank,
	_validate_announcement,
	search_school_admin_announcement_students_data,
)


class TestSingleStudentAnnouncements(TestCase):
	def test_single_student_audience_requires_student(self):
		doc = frappe._dict(title="Test", body="Message", audience_type="Single Student", student="")

		with self.assertRaises(frappe.ValidationError):
			_validate_announcement(doc)

	@patch("qas_custom.services.announcements._recipient_from_parent_name")
	@patch("qas_custom.services.announcements._student_parent", return_value="PAR-1")
	def test_single_student_resolves_exactly_one_family(self, _parent, recipient_from_parent):
		recipient_from_parent.return_value = {
			"parent": "PAR-1",
			"student": "STU-1",
			"email": "parent@example.com",
		}
		db = SimpleNamespace(exists=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			result = _resolve_announcement_recipients(
				frappe._dict(audience_type="Single Student", student="STU-1")
			)

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["parent"], "PAR-1")
		recipient_from_parent.assert_called_once_with(
			"PAR-1",
			student="STU-1",
			audience_source="Single Student",
			source_document="STU-1",
		)

	@patch("qas_custom.services.announcements._student_parent", return_value=None)
	def test_single_student_without_parent_is_blocked(self, _parent):
		db = SimpleNamespace(exists=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			with self.assertRaises(frappe.ValidationError):
				_resolve_announcement_recipients(
					frappe._dict(audience_type="Single Student", student="STU-1")
				)

	def test_student_search_prefers_exact_then_prefix_matches(self):
		rows = [
			frappe._dict(name="STU-3", student_name="Alexa Young"),
			frappe._dict(name="STU-2", student_name="Alexandra Young"),
			frappe._dict(name="STU-1", student_name="Alex"),
		]

		ordered = sorted(rows, key=lambda row: _student_search_rank(row, "Alex"))

		self.assertEqual([row.name for row in ordered], ["STU-1", "STU-3", "STU-2"])

	@patch("qas_custom.services.announcements._announcement_student_preview", side_effect=lambda row: {"student": row.name})
	@patch("qas_custom.services.announcements._student_search_fields", return_value=["name", "student_name"])
	@patch("qas_custom.services.announcements._require_school_admin")
	@patch("qas_custom.services.announcements.frappe.get_all")
	def test_server_search_combines_exact_prefix_and_contains_results(self, get_all, _require, _fields, _preview):
		get_all.side_effect = [
			[frappe._dict(name="STU-1", student_name="Alex")],
			[frappe._dict(name="STU-2", student_name="Alexa")],
			[frappe._dict(name="STU-3", student_name="Sam Alex")],
		]

		result = search_school_admin_announcement_students_data("Alex", 20)

		self.assertEqual([row["student"] for row in result["items"]], ["STU-1", "STU-2", "STU-3"])
		self.assertEqual(get_all.call_count, 3)

	def test_student_preview_uses_family_identity_and_email(self):
		parent = frappe._dict(
			name="PAR-1",
			parent_name="Parent One",
			customer="CUS-1",
			linked_user="parent@example.com",
			email="parent@example.com",
		)
		db = SimpleNamespace(get_value=Mock(return_value=parent), has_column=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			result = _announcement_student_preview(
				frappe._dict(
					name="STU-1",
					student_name="Student One",
					guardian="PAR-1",
					student_code="S001",
					status="Active",
				)
			)

		self.assertTrue(result["eligible"])
		self.assertEqual(result["parent"], "PAR-1")
		self.assertEqual(result["parent_name"], "Parent One")
		self.assertEqual(result["parent_email"], "parent@example.com")
		self.assertEqual(result["customer"], "CUS-1")
