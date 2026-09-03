from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.workshops import (
	ATTENDANCE_STATUSES,
	activate_school_admin_workshop_enrollment_data,
	_update_attendance,
)


class TestWorkshops(TestCase):
	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops.get_student_parent_name", return_value="Student One")
	def test_activation_creates_only_missing_attendance_rows(
		self, _student_name, workshop_frappe, required_doc, _require
	):
		enrollment = Mock()
		enrollment.name = "WEN-1"
		enrollment.status = "Planned"
		enrollment.workshop_offering = "WSO-1"
		enrollment.student = "STU-1"
		enrollment.get.side_effect = lambda field: {
			"name": "WEN-1", "workshop_offering": "WSO-1", "student": "STU-1", "status": enrollment.status
		}.get(field)
		required_doc.return_value = enrollment
		workshop_frappe.get_all.return_value = [SimpleNamespace(name="WSS-1"), SimpleNamespace(name="WSS-2")]
		workshop_frappe.db.exists.side_effect = [True, False]
		attendance = Mock()
		workshop_frappe.get_doc.return_value = attendance

		result = activate_school_admin_workshop_enrollment_data("WEN-1")

		self.assertEqual(result["attendance_entries"], 1)
		self.assertEqual(enrollment.status, "Active")
		enrollment.save.assert_called_once_with(ignore_permissions=True)
		attendance.insert.assert_called_once_with(ignore_permissions=True)

	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_attendance_rejects_status_from_recurring_leave_workflow(self, _translate, workshop_frappe, required_doc):
		row = Mock()
		row.workshop_session = "WSS-1"
		required_doc.return_value = row
		workshop_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			_update_attendance("WSS-1", [{"row_id": "WAT-1", "status": "Leave"}])

		self.assertNotIn("Leave", ATTENDANCE_STATUSES)

	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_attendance_rejects_row_from_another_workshop_session(self, _translate, workshop_frappe, required_doc):
		row = Mock()
		row.workshop_session = "WSS-OTHER"
		required_doc.return_value = row
		workshop_frappe.throw.side_effect = frappe.PermissionError

		with self.assertRaises(frappe.PermissionError):
			_update_attendance("WSS-1", [{"row_id": "WAT-1", "status": "Present"}])
