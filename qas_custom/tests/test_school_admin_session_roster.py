from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin import (
	_enrich_trial_confirmation_status,
	_roster_course_session_attendance_rows,
	_visible_course_session_attendance_rows,
)


class TestSchoolAdminSessionRoster(TestCase):
	def test_non_attending_rows_stay_in_the_admin_roster_but_not_the_attending_count(self):
		rows = [
			{"student": "Attending", "status": "To be started"},
			{"student": "On leave", "status": "Leave"},
			{"student": "Cancelled", "status": "Cancelled"},
		]

		self.assertEqual(
			[row["student"] for row in _roster_course_session_attendance_rows(rows)],
			["Attending", "On leave", "Cancelled"],
		)
		self.assertEqual([row["student"] for row in _visible_course_session_attendance_rows(rows)], ["Attending"])

	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_trial_confirmation_status_is_loaded_in_one_batch_for_all_roster_rows(self, get_all):
		get_all.return_value = [
			{"name": "INQ-PENDING", "confirmation_status": "Pending"},
			{"name": "INQ-CONFIRMED", "confirmation_status": "Customer Confirmed"},
			{"name": "INQ-LEGACY", "confirmation_status": "Sent"},
		]
		rows = [
			{"student": "Pending", "source_doctype": "Inquiry", "source_document": "INQ-PENDING"},
			{
				"student": "Confirmed leave",
				"status": "Leave",
				"source_doctype": "Inquiry",
				"source_document": "INQ-CONFIRMED",
			},
			{"student": "Legacy", "source_doctype": "Inquiry", "source_document": "INQ-LEGACY"},
			{"student": "Missing", "source_doctype": "Inquiry", "source_document": "INQ-MISSING"},
			{"student": "Full term", "source_doctype": "Program Enrollment", "source_document": "ENR-1"},
		]

		_enrich_trial_confirmation_status(rows)

		get_all.assert_called_once_with(
			"Inquiry",
			filters={
				"name": ["in", ["INQ-CONFIRMED", "INQ-LEGACY", "INQ-MISSING", "INQ-PENDING"]]
			},
			fields=["name", "confirmation_status"],
			limit_page_length=0,
		)
		self.assertEqual(rows[0]["trial_confirmation_status"], "Pending")
		self.assertEqual(rows[1]["trial_confirmation_status"], "Customer Confirmed")
		self.assertEqual(rows[2]["trial_confirmation_status"], "")
		self.assertEqual(rows[3]["trial_confirmation_status"], "")
		self.assertNotIn("trial_confirmation_status", rows[4])

	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_non_trial_roster_does_not_query_inquiries(self, get_all):
		rows = [{"student": "Full term", "source_doctype": "Program Enrollment"}]

		self.assertIs(_enrich_trial_confirmation_status(rows), rows)

		get_all.assert_not_called()
