from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.attendance.commands import ensure_trial_inquiry_attendance_entry
from qas_custom.services.class_attendance import create_attendance_entry


class TestCancelledTrialAttendanceReactivation(TestCase):
	@patch("qas_custom.modules.attendance.commands.create_attendance_entry")
	@patch("qas_custom.modules.attendance.commands.get_attendance_entry_by_source", return_value=None)
	def test_trial_inquiry_enables_cancelled_attendance_reactivation(self, _get_by_source, create_entry):
		inquiry = SimpleNamespace(
			inquiry_type="Trial Lesson",
			status="Booked",
			course_session="SESSION-001",
			student="STUDENT-001",
			name="INQ-NEW",
		)

		ensure_trial_inquiry_attendance_entry(inquiry)

		self.assertTrue(create_entry.call_args.kwargs["prevent_student_duplicate"])
		self.assertTrue(create_entry.call_args.kwargs["reactivate_cancelled_duplicate"])

	def test_reuses_cancelled_trial_attendance_for_current_inquiry(self):
		db = SimpleNamespace(
			get_value=Mock(
				return_value={
					"name": "ATT-001",
					"status": "Cancelled",
					"enrollment_type": "Trial",
				}
			)
		)
		attendance = Mock()
		attendance.name = "ATT-001"
		attendance.get.side_effect = lambda fieldname: {"status": "Cancelled"}.get(fieldname)
		attendance.meta.has_field.return_value = True
		fake_frappe = SimpleNamespace(
			db=db,
			get_doc=Mock(return_value=attendance),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)

		with patch("qas_custom.services.class_attendance.frappe", fake_frappe):
			result = create_attendance_entry(
				course_session="SESSION-001",
				student="STUDENT-001",
				enrollment_type="Trial",
				source_doctype="Inquiry",
				source_document="INQ-NEW",
				status="To be started",
				comments="Added from Inquiry INQ-NEW",
				prevent_student_duplicate=True,
				reactivate_cancelled_duplicate=True,
			)

		self.assertEqual(result, "ATT-001")
		fake_frappe.get_doc.assert_called_once_with("Class Attendance Entry", "ATT-001")
		self.assertEqual(attendance.previous_status, "Cancelled")
		self.assertEqual(attendance.status, "To be started")
		self.assertEqual(attendance.comments, "Added from Inquiry INQ-NEW")
		self.assertEqual(attendance.source_doctype, "Inquiry")
		self.assertEqual(attendance.source_document, "INQ-NEW")
		attendance.set.assert_any_call("marked_by", None)
		attendance.set.assert_any_call("marked_at", None)
		attendance.save.assert_called_once_with(ignore_permissions=True)

	def test_non_cancelled_attendance_still_blocks_duplicate(self):
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(
				get_value=Mock(
					return_value={
						"name": "ATT-001",
						"status": "To be started",
						"enrollment_type": "Trial",
					}
				)
			),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)

		with patch("qas_custom.services.class_attendance.frappe", fake_frappe), patch(
			"qas_custom.services.class_attendance._", side_effect=lambda message: message
		):
			with self.assertRaisesRegex(RuntimeError, "already listed"):
				create_attendance_entry(
					course_session="SESSION-001",
					student="STUDENT-001",
					enrollment_type="Trial",
					source_doctype="Inquiry",
					source_document="INQ-NEW",
					prevent_student_duplicate=True,
					reactivate_cancelled_duplicate=True,
				)

	def test_cancelled_non_trial_attendance_is_not_reassigned(self):
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(
				get_value=Mock(
					return_value={
						"name": "ATT-001",
						"status": "Cancelled",
						"enrollment_type": "Full-Term",
					}
				)
			),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)

		with patch("qas_custom.services.class_attendance.frappe", fake_frappe), patch(
			"qas_custom.services.class_attendance._", side_effect=lambda message: message
		):
			with self.assertRaisesRegex(RuntimeError, "already listed"):
				create_attendance_entry(
					course_session="SESSION-001",
					student="STUDENT-001",
					enrollment_type="Trial",
					prevent_student_duplicate=True,
					reactivate_cancelled_duplicate=True,
				)

	def test_no_existing_attendance_creates_new_row(self):
		db = SimpleNamespace(get_value=Mock(side_effect=[None, None, None]))
		attendance = Mock()
		attendance.name = "ATT-NEW"
		attendance.meta.has_field.return_value = True
		fake_frappe = SimpleNamespace(
			db=db,
			new_doc=Mock(return_value=attendance),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)

		with patch("qas_custom.services.class_attendance.frappe", fake_frappe):
			result = create_attendance_entry(
				course_session="SESSION-001",
				student="STUDENT-001",
				enrollment_type="Trial",
				source_doctype="Inquiry",
				source_document="INQ-NEW",
				prevent_student_duplicate=True,
				reactivate_cancelled_duplicate=True,
			)

		self.assertEqual(result, "ATT-NEW")
		fake_frappe.new_doc.assert_called_once_with("Class Attendance Entry")
		attendance.insert.assert_called_once_with(ignore_permissions=True)
