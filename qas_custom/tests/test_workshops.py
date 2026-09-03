from datetime import time, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.workshops import (
	ATTENDANCE_STATUSES,
	activate_school_admin_workshop_enrollment_data,
	duplicate_school_admin_workshop_offering_data,
	_enrollment_payload,
	_serialise_workshop_time,
	_workshop_invoice_item,
	_update_attendance,
)


class TestWorkshops(TestCase):
	@patch("qas_custom.services.workshops.frappe")
	def test_workshop_invoice_item_prefers_configured_item(self, workshop_frappe):
		workshop_frappe.conf.get.side_effect = ["Configured Workshop Fee"]
		workshop_frappe.db.exists.return_value = True

		self.assertEqual(_workshop_invoice_item(), "Configured Workshop Fee")
		workshop_frappe.db.exists.assert_called_once_with("Item", "Configured Workshop Fee")

	@patch("qas_custom.services.workshops.frappe")
	def test_workshop_invoice_item_falls_back_to_existing_workshop_fee(self, workshop_frappe):
		workshop_frappe.conf.get.side_effect = [None, None]
		workshop_frappe.db.exists.return_value = True

		self.assertEqual(_workshop_invoice_item(), "Workshop Fee")
		workshop_frappe.db.exists.assert_called_once_with("Item", "Workshop Fee")

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_workshop_invoice_item_rejects_missing_configured_item(self, _translate, workshop_frappe):
		workshop_frappe.conf.get.side_effect = ["Missing Item"]
		workshop_frappe.db.exists.return_value = False
		workshop_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			_workshop_invoice_item()

		workshop_frappe.throw.assert_called_once_with("Configured Workshop invoice Item Missing Item does not exist.")

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops.get_student_parent_name", return_value="Student One")
	def test_enrollment_payload_includes_student_and_parent_display_names(self, _student_name, workshop_frappe):
		workshop_frappe.db.get_value.return_value = "Parent One"

		payload = _enrollment_payload(frappe._dict(name="WEN-1", student="STU-1", parent="PAR-1"))

		self.assertEqual(payload["student_name"], "Student One")
		self.assertEqual(payload["parent_name"], "Parent One")
		workshop_frappe.db.get_value.assert_called_once_with("Parent", "PAR-1", "parent_name")

	def test_workshop_time_serialization_zero_pads_morning_hours(self):
		self.assertEqual(_serialise_workshop_time("9:30:00"), "09:30:00")
		self.assertEqual(_serialise_workshop_time(timedelta(hours=9, minutes=30)), "09:30:00")
		self.assertEqual(_serialise_workshop_time(time(16, 0)), "16:00:00")
		self.assertEqual(_serialise_workshop_time(None), "")

	@patch("qas_custom.services.workshops._build_offering_detail", return_value={"name": "WSO-COPY"})
	@patch("qas_custom.services.workshops._sync_session_positions")
	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_duplicate_copies_offering_and_sessions_only(
		self, _translate, workshop_frappe, required_doc, _require, sync_positions, build_detail
	):
		source = frappe._dict(
			name="WSO-ORIGINAL", title="Watercolour Day",
			workshop_category="General Workshop", participation_mode="Individual",
			class_language="English", campus="Indooroopilly", description="Paint",
			minimum_age=12, maximum_age=99, standard_price=200, capacity=15,
			materials_or_preparation="Brushes", inclusions="Materials",
		)
		required_doc.return_value = source
		workshop_frappe.get_all.return_value = [
			frappe._dict(session_date="2026-09-21", start_time="9:30:00", end_time="16:00:00", teacher="TEA-1", classroom="Room 1", status="Scheduled"),
			frappe._dict(session_date="2026-09-22", start_time="9:30:00", end_time="16:00:00", teacher=None, classroom=None, status="Scheduled"),
		]
		copy = Mock(name="copy")
		copy.name = "WSO-COPY"
		copy.set.side_effect = lambda field, value: setattr(copy, field, value)
		session_one = Mock(name="session_one")
		session_one.set.side_effect = lambda field, value: setattr(session_one, field, value)
		session_two = Mock(name="session_two")
		session_two.set.side_effect = lambda field, value: setattr(session_two, field, value)
		workshop_frappe.new_doc.side_effect = [copy, session_one, session_two]

		result = duplicate_school_admin_workshop_offering_data("WSO-ORIGINAL")

		self.assertEqual(copy.title, "Copy of Watercolour Day")
		self.assertEqual(copy.status, "Draft")
		self.assertEqual(copy.standard_price, 200)
		self.assertEqual(copy.capacity, 15)
		copy.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(session_one.workshop_offering, "WSO-COPY")
		self.assertEqual(session_one.teacher, "TEA-1")
		self.assertIsNone(session_two.teacher)
		session_one.insert.assert_called_once_with(ignore_permissions=True)
		session_two.insert.assert_called_once_with(ignore_permissions=True)
		sync_positions.assert_called_once_with("WSO-COPY")
		workshop_frappe.db.commit.assert_called_once()
		workshop_frappe.db.rollback.assert_not_called()
		self.assertEqual(result["source"], "WSO-ORIGINAL")
		self.assertEqual(result["offering"], {"name": "WSO-COPY"})
		build_detail.assert_called_once_with(copy)
		self.assertEqual(workshop_frappe.get_all.call_args.args[0], "Workshop Session")

	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_duplicate_rolls_back_when_session_copy_fails(self, _translate, workshop_frappe, required_doc, _require):
		required_doc.return_value = frappe._dict(name="WSO-ORIGINAL", title="Workshop")
		workshop_frappe.get_all.return_value = [frappe._dict(session_date="2026-09-21")]
		copy = Mock(name="copy")
		copy.name = "WSO-COPY"
		copy.set.side_effect = lambda field, value: setattr(copy, field, value)
		session = Mock(name="session")
		session.set.side_effect = lambda field, value: setattr(session, field, value)
		session.insert.side_effect = RuntimeError("session failed")
		workshop_frappe.new_doc.side_effect = [copy, session]

		with self.assertRaisesRegex(RuntimeError, "session failed"):
			duplicate_school_admin_workshop_offering_data("WSO-ORIGINAL")

		workshop_frappe.db.rollback.assert_called_once_with(save_point="duplicate_workshop_offering")
		workshop_frappe.db.commit.assert_not_called()

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
