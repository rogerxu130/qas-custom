from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.weekly_timeslot_course_change import _preview


class TestSchoolAdminWeeklyTimeslotCourseChange(TestCase):
	@patch("qas_custom.services.weekly_timeslot_course_change.today", return_value="2026-09-21")
	@patch("qas_custom.services.weekly_timeslot_course_change.frappe.get_all")
	@patch("qas_custom.services.weekly_timeslot_course_change.frappe.get_doc")
	def test_preview_lists_enrollments_invoices_and_trials_without_writes(self, get_doc, get_all, _today):
		doc = frappe._dict(name="WTS-2026-00010", term="Term 4 2026", course="Course A")
		get_doc.return_value = doc
		sessions = [frappe._dict(name="CS-1", session_date="2026-10-10", status="Scheduled")]
		enrollments = [
			frappe._dict(name="ENR-1", status="Planned", course="Course A", invoice="INV-1", invoice_status="Draft", invoice_amount=100),
			frappe._dict(name="ENR-2", status="Active", course="Course A", invoice=None, invoice_status=None, invoice_amount=0),
		]
		get_all.side_effect = [sessions, enrollments, ["INQ-1"]]
		db = Mock()
		db.exists.side_effect = lambda doctype, filters=None: True if doctype == "Course" else None
		db.get_value.side_effect = [frappe._dict(status="Upcoming", start_date="2026-10-05"), 90, 120]
		with patch("qas_custom.services.weekly_timeslot_course_change.frappe.db", new=db), patch(
			"qas_custom.services.weekly_timeslot_course_change._", side_effect=lambda value: value
		):
			result = _preview(doc.name, "Course B")

		self.assertEqual(result["planned_enrollment_count"], 1)
		self.assertEqual(result["active_enrollment_count"], 1)
		self.assertEqual(result["draft_invoices"], ["INV-1"])
		self.assertEqual(result["trial_inquiries"], ["INQ-1"])
		self.assertTrue(result["duration_changed"])
		self.assertFalse(result["blocking_errors"])
		db.set_value.assert_not_called()
		db.commit.assert_not_called()
