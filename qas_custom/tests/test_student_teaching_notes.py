from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.campus_admin import (
	_assert_campus_admin_student_access,
	update_campus_admin_student_teaching_notes_data,
)
from qas_custom.services.school_admin import (
	_get_student_teaching_notes_map,
	_normalize_student_teaching_notes,
)
from qas_custom.services.teacher_portal import get_teacher_session_detail_data


class TestStudentTeachingNotes(TestCase):
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

	@patch("qas_custom.services.campus_admin._assert_campus_admin_student_access")
	@patch("qas_custom.services.campus_admin._safe_fields", return_value=["teaching_notes"])
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin.reject_support_view_write")
	def test_campus_admin_can_save_trimmed_note_for_accessible_student(
		self,
		reject_support,
		require_profile,
		_safe_fields,
		assert_access,
	):
		require_profile.return_value = {"campuses": ["Campus A"]}
		doc = Mock()
		doc.name = "STU-1"
		doc.get.side_effect = lambda fieldname: getattr(doc, fieldname, None)
		db = Mock()
		db.exists.return_value = True
		fake_frappe = Mock()
		fake_frappe.db = db
		fake_frappe.get_doc.return_value = doc

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			result = update_campus_admin_student_teaching_notes_data("STU-1", "  Avoid witch themes.  ")

		reject_support.assert_called_once_with()
		assert_access.assert_called_once_with("STU-1", ["Campus A"])
		self.assertEqual(doc.teaching_notes, "Avoid witch themes.")
		doc.save.assert_called_once_with(ignore_permissions=True)
		db.commit.assert_called_once_with()
		self.assertEqual(result, {"student": "STU-1", "teaching_notes": "Avoid witch themes."})

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

	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch(
		"qas_custom.services.campus_admin.reject_support_view_write",
		side_effect=frappe.PermissionError,
	)
	def test_support_view_cannot_update_teaching_notes(self, _reject_support, require_profile):
		with self.assertRaises(frappe.PermissionError):
			update_campus_admin_student_teaching_notes_data("STU-1", "note")

		require_profile.assert_not_called()

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
