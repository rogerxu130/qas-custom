from unittest import TestCase

from qas_custom.services.school_admin import (
	_roster_course_session_attendance_rows,
	_visible_course_session_attendance_rows,
)


class TestSchoolAdminSessionRoster(TestCase):
	def test_leave_rows_stay_in_the_admin_roster_but_not_the_attending_count(self):
		rows = [
			{"student": "Attending", "status": "To be started"},
			{"student": "On leave", "status": "Leave"},
			{"student": "Cancelled", "status": "Cancelled"},
		]

		self.assertEqual([row["student"] for row in _roster_course_session_attendance_rows(rows)], ["Attending", "On leave"])
		self.assertEqual([row["student"] for row in _visible_course_session_attendance_rows(rows)], ["Attending"])
