from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

import qas_custom.api.campus_admin as campus_admin_api
from qas_custom.services.campus_admin import (
	_assert_campus_admin_student_access,
)
from qas_custom.services.parent_portal_write import update_parent_student_teaching_notes_data
from qas_custom.services.school_admin import (
	_get_student_teaching_notes_map,
	_normalize_student_teaching_notes,
)
from qas_custom.services.teacher_portal import (
	_count_special_students,
	_count_students_with_teaching_notes,
	get_teacher_session_detail_data,
)


class TestStudentTeachingNotes(TestCase):
	def test_teacher_session_summary_counts_students_with_special_needs(self):
		count = _count_students_with_teaching_notes(
			[
				{"student": "STU-1"},
				{"student": "STU-2"},
				{"student": "STU-3"},
			],
			{
				"STU-1": {"teaching_notes": "Avoid witch themes."},
				"STU-2": {"teaching_notes": "   "},
				"STU-3": {"teaching_notes": None},
			},
		)

		self.assertEqual(count, 1)

	def test_teacher_session_summary_counts_first_class_after_transfer(self):
		counts = _count_special_students(
			[
				{"enrollment_type": "Full-Term", "qas_first_class_after_transfer": 1},
				{"enrollment_type": "Full-Term", "qas_first_class_after_transfer": 0},
				{"enrollment_type": "Trial", "qas_first_class_after_transfer": 0},
			]
		)

		self.assertEqual(counts["first_class_after_transfer"], 1)
		self.assertEqual(counts["trial"], 1)

	def test_school_admin_payload_trims_teaching_notes(self):
		payload = {"student_name": "Student", "teaching_notes": "  No Christmas activities.  "}

		_normalize_student_teaching_notes(payload)

		self.assertEqual(payload["teaching_notes"], "No Christmas activities.")

	@patch("qas_custom.services.school_admin._safe_fields", return_value=["name", "teaching_notes"])
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_roster_note_map_is_batched_and_trimmed(self, get_all, _safe_fields):
		get_all.return_value = [
			{"name": "STU-1", "teaching_notes": "  Quiet space if overwhelmed.  "},
			{"name": "STU-2", "teaching_notes": None},
		]

		result = _get_student_teaching_notes_map(["STU-2", "STU-1", "STU-1"])

		self.assertEqual(result, {"STU-1": "Quiet space if overwhelmed.", "STU-2": ""})
		get_all.assert_called_once_with(
			"Student",
			filters={"name": ["in", ["STU-1", "STU-2"]]},
			fields=["name", "teaching_notes"],
			limit_page_length=0,
		)

	@patch("qas_custom.services.parent_portal_write._get_parent_students")
	@patch("qas_custom.services.parent_portal_write._require_parent")
	@patch("qas_custom.services.parent_portal_write.reject_support_view_write")
	def test_parent_can_save_trimmed_note_for_own_student(
		self,
		reject_support,
		require_parent,
		get_parent_students,
	):
		require_parent.return_value = frappe._dict(name="PARENT-1")
		get_parent_students.return_value = [{"name": "STU-1"}]
		doc = Mock()
		doc.name = "STU-1"
		db = Mock()
		db.has_column.return_value = True
		fake_frappe = Mock()
		fake_frappe.db = db
		fake_frappe.get_doc.return_value = doc

		with patch("qas_custom.services.parent_portal_write.frappe", fake_frappe):
			result = update_parent_student_teaching_notes_data("STU-1", "  Avoid witch themes.  ")

		reject_support.assert_called_once_with()
		self.assertEqual(doc.teaching_notes, "Avoid witch themes.")
		doc.save.assert_called_once_with(ignore_permissions=True)
		db.commit.assert_called_once_with()
		self.assertEqual(result, {"student": "STU-1", "teaching_notes": "Avoid witch themes."})

	@patch("qas_custom.services.parent_portal_write._get_parent_students")
	@patch("qas_custom.services.parent_portal_write._require_parent")
	@patch("qas_custom.services.parent_portal_write.reject_support_view_write")
	def test_parent_cannot_save_note_longer_than_eighty_characters(
		self,
		reject_support,
		require_parent,
		get_parent_students,
	):
		require_parent.return_value = frappe._dict(name="PARENT-1")
		get_parent_students.return_value = [{"name": "STU-1"}]
		db = Mock()
		db.has_column.return_value = True
		fake_frappe = Mock()
		fake_frappe.db = db
		fake_frappe.throw.side_effect = frappe.ValidationError

		with patch("qas_custom.services.parent_portal_write.frappe", fake_frappe):
			with self.assertRaises(frappe.ValidationError):
				update_parent_student_teaching_notes_data("STU-1", "x" * 81)

		reject_support.assert_called_once_with()
		fake_frappe.get_doc.assert_not_called()
		fake_frappe.throw.assert_called_once_with(
			"Special needs / important classroom notes must be 80 characters or fewer."
		)

	def test_campus_admin_note_mutation_endpoint_is_not_exposed(self):
		self.assertFalse(hasattr(campus_admin_api, "campus_admin_update_student_teaching_notes"))

	@patch("qas_custom.services.campus_admin.frappe.get_all")
	def test_campus_admin_student_access_rejects_cross_campus_student(self, get_all):
		get_all.side_effect = [["SESSION-1"], ["TIMESLOT-1"], []]

		with patch(
			"qas_custom.services.campus_admin.frappe.throw",
			side_effect=frappe.PermissionError,
		) as throw:
			with self.assertRaises(frappe.PermissionError):
				_assert_campus_admin_student_access("STU-1", ["Campus A"])

		throw.assert_called_once()

	@patch(
		"qas_custom.services.parent_portal_write.reject_support_view_write",
		side_effect=frappe.PermissionError,
	)
	def test_support_view_cannot_update_teaching_notes(self, _reject_support):
		with self.assertRaises(frappe.PermissionError):
			update_parent_student_teaching_notes_data("STU-1", "note")


	@patch("qas_custom.services.teacher_portal._count_leave_rows", return_value=0)
	@patch("qas_custom.services.teacher_portal._count_special_students", return_value=0)
	@patch("qas_custom.services.teacher_portal.get_makeup_voucher_label", return_value="")
	@patch("qas_custom.services.teacher_portal.get_student_display_name", return_value="Student")
	@patch("qas_custom.services.teacher_portal._get_attendance_status_options", return_value=[])
	@patch("qas_custom.services.teacher_portal._get_video_post_rows", return_value=[])
	@patch("qas_custom.services.teacher_portal._get_photo_post_rows", return_value=[])
	@patch("qas_custom.services.teacher_portal._get_homework_rows", return_value=[])
	@patch("qas_custom.services.teacher_portal._get_parent_contact_map", return_value={})
	@patch("qas_custom.services.teacher_portal._get_student_map")
	@patch("qas_custom.services.teacher_portal._visible_attendance_rows")
	@patch("qas_custom.services.teacher_portal._get_attendance_rows")
	@patch("qas_custom.services.teacher_portal._get_timeslot")
	@patch("qas_custom.services.teacher_portal._get_owned_session")
	@patch("qas_custom.services.teacher_portal._require_teacher")
	def test_teacher_session_detail_exposes_note_for_owned_roster_student(
		self,
		_require_teacher,
		get_owned_session,
		get_timeslot,
		get_attendance_rows,
		visible_rows,
		get_student_map,
		*_unused,
	):
		get_owned_session.return_value = frappe._dict(
			name="SESSION-1",
			weekly_timeslot="TIMESLOT-1",
			date="2026-07-17",
			teacher_override="",
			status="Scheduled",
		)
		get_timeslot.return_value = frappe._dict(
			name="TIMESLOT-1",
			teacher="TEACHER-1",
			course="COURSE-1",
			campus="Campus A",
			classroom="Room 1",
			start_time="15:00:00",
			end_time="16:00:00",
		)
		row = {
			"name": "ATT-1",
			"student": "STU-1",
			"status": "To be started",
			"enrollment_type": "Full Term",
		}
		get_attendance_rows.return_value = [row]
		visible_rows.return_value = [row]
		get_student_map.return_value = {
			"STU-1": {
				"name": "STU-1",
				"student_name": "Student",
				"teaching_notes": "No Christmas activities.",
			}
		}

		payload = get_teacher_session_detail_data("SESSION-1")

		self.assertEqual(payload["students"][0]["teaching_notes"], "No Christmas activities.")
		self.assertFalse(payload["students"][0]["first_class_after_transfer"])
