from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.parent_emails import (
	_effective_teacher,
	_parent_recipient,
	_session_recipients,
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
