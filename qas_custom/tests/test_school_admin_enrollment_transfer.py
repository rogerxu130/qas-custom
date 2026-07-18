from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import getdate

from qas_custom.services.school_admin import (
	_build_enrollment_transfer_preview,
	_cancel_future_enrollment_attendance,
	_ensure_transfer_destination_attendance,
	transfer_school_admin_enrollment_data,
)


class TestSchoolAdminEnrollmentTransfer(TestCase):
	def _enrollment(self):
		return SimpleNamespace(
			name="ENR-001",
			student="STU-001",
			weekly_timeslot="WT-OLD",
			term="TERM-3",
			status="Active",
			enrollment_type="Full-Term",
			get=lambda field: {
				"student": "STU-001",
				"weekly_timeslot": "WT-OLD",
				"term": "TERM-3",
				"status": "Active",
				"enrollment_type": "Full-Term",
			}.get(field),
		)

	@patch("qas_custom.services.school_admin._existing_target_enrollment", return_value=None)
	@patch("qas_custom.services.school_admin.frappe.get_all")
	@patch("qas_custom.services.school_admin.frappe.db.get_value")
	@patch("qas_custom.services.school_admin.frappe.db.exists", return_value=True)
	def test_preview_separates_unmarked_and_marked_attendance(self, _exists, get_value, get_all, _duplicate):
		get_value.return_value = {"name": "WT-NEW", "term": "TERM-3", "course": "COURSE-NEW", "status": "Active"}
		get_all.side_effect = [
			[
				{"name": "CS-OLD-1", "session_date": getdate("2026-07-20"), "status": "Scheduled"},
				{"name": "CS-OLD-2", "session_date": getdate("2026-07-27"), "status": "Completed"},
			],
			[
				{"name": "CS-NEW-1", "session_date": getdate("2026-07-21"), "status": "Scheduled"},
				{"name": "CS-NEW-2", "session_date": getdate("2026-07-28"), "status": "Scheduled"},
			],
			[
				{"name": "ATT-1", "course_session": "CS-OLD-1", "status": "To be started"},
				{"name": "ATT-2", "course_session": "CS-OLD-2", "status": "Present"},
			],
		]

		result = _build_enrollment_transfer_preview(self._enrollment(), "WT-NEW", getdate("2026-07-20"))

		self.assertEqual(result["cancellable_count"], 1)
		self.assertEqual(result["destination_session_count"], 1)
		self.assertEqual(result["destination_sessions_skipped_for_marked_count"], 1)
		self.assertEqual(result["target_session_ids"], ["CS-NEW-1"])
		self.assertEqual(result["retained_marked_count"], 1)
		self.assertEqual(result["retained_marked_rows"][0]["status"], "Present")

	@patch("qas_custom.services.school_admin._cancel_enrollment_attendance_for_sessions")
	@patch("qas_custom.services.school_admin._build_enrollment_transfer_preview")
	@patch("qas_custom.services.school_admin.frappe.get_doc")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_execution_requires_confirmation_when_marked_attendance_will_be_retained(
		self,
		_require,
		get_doc,
		build_preview,
		cancel_attendance,
	):
		get_doc.return_value = self._enrollment()
		build_preview.return_value = {"retained_marked_count": 1, "preview_fingerprint": "preview-1"}

		with self.assertRaises(frappe.ValidationError):
			transfer_school_admin_enrollment_data(
				enrollment="ENR-001",
				payload={
					"weekly_timeslot": "WT-NEW",
					"effective_date": "2026-07-20",
					"preview_fingerprint": "preview-1",
				},
			)

		cancel_attendance.assert_not_called()

	@patch("qas_custom.services.school_admin._cancel_enrollment_attendance_for_sessions")
	@patch("qas_custom.services.school_admin._build_enrollment_transfer_preview")
	@patch("qas_custom.services.school_admin.frappe.get_doc")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_execution_rejects_a_stale_preview(
		self,
		_require,
		get_doc,
		build_preview,
		cancel_attendance,
	):
		get_doc.return_value = self._enrollment()
		build_preview.return_value = {"retained_marked_count": 0, "preview_fingerprint": "current"}

		with self.assertRaises(frappe.ValidationError):
			transfer_school_admin_enrollment_data(
				enrollment="ENR-001",
				payload={
					"weekly_timeslot": "WT-NEW",
					"effective_date": "2026-07-20",
					"preview_fingerprint": "old",
				},
			)

		cancel_attendance.assert_not_called()

	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe.get_all", return_value=[])
	@patch("qas_custom.services.school_admin.frappe.db.set_value")
	def test_future_cancel_does_not_fall_back_to_all_attendance_when_no_sessions_exist(self, set_value, _get_all, _available):
		result = _cancel_future_enrollment_attendance("ENR-001", effective_date="2099-01-01")

		self.assertEqual(result, 0)
		set_value.assert_not_called()

	@patch("qas_custom.services.school_admin.create_attendance_entry")
	@patch("qas_custom.services.school_admin.frappe.db.set_value")
	@patch("qas_custom.services.school_admin.frappe.db.get_value")
	def test_destination_attendance_reactivates_cancelled_and_creates_missing(self, get_value, set_value, create_entry):
		get_value.side_effect = [
			{"name": "ATT-CANCELLED", "status": "Cancelled"},
			None,
		]

		result = _ensure_transfer_destination_attendance(self._enrollment(), ["CS-NEW-1", "CS-NEW-2"])

		self.assertEqual(result, {"created": 1, "reactivated": 1, "retained": 0, "total": 2})
		set_value.assert_called_once_with(
			"Class Attendance Entry",
			"ATT-CANCELLED",
			"status",
			"To be started",
			update_modified=True,
		)
		self.assertEqual(create_entry.call_count, 1)
		self.assertEqual(create_entry.call_args.kwargs["course_session"], "CS-NEW-2")
