from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import _validate_weekly_timeslot_change


def slot(**updates):
	values = dict(
		name="WTS-2026-00010",
		term="Term 4 2026",
		course="Course A",
		campus="Indooroopilly",
		day_of_week="Saturday",
		start_time="14:00:00",
		end_time="15:30:00",
	)
	values.update(updates)
	return frappe._dict(values)


class TestSchoolAdminWeeklyTimeslotChanges(TestCase):
	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=RuntimeError)
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_generated_sessions_require_schedule_confirmation_and_effective_date(self, _translate, _throw):
		db = Mock()
		db.exists.side_effect = lambda doctype, filters: "CS-1" if doctype == "Course Sessions" else None
		with patch("qas_custom.services.school_admin.frappe.db", new=db), self.assertRaises(RuntimeError):
			_validate_weekly_timeslot_change(slot(), {"day_of_week": "Sunday"})

		with patch("qas_custom.services.school_admin.frappe.db", new=db), self.assertRaises(RuntimeError):
			_validate_weekly_timeslot_change(slot(), {"day_of_week": "Sunday", "apply_future_sessions": 1})

		with patch("qas_custom.services.school_admin.frappe.db", new=db):
			_validate_weekly_timeslot_change(
				slot(),
				{"day_of_week": "Sunday", "apply_future_sessions": 1, "effective_date": "2026-10-05"},
			)

	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=RuntimeError)
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_populated_class_rejects_ordinary_course_edit(self, _translate, _throw):
		db = Mock()
		db.exists.side_effect = lambda doctype, filters: "CS-1" if doctype == "Course Sessions" else None
		with patch("qas_custom.services.school_admin.frappe.db", new=db), self.assertRaises(RuntimeError):
			_validate_weekly_timeslot_change(slot(), {"course": "Course B"})

	@patch("qas_custom.services.school_admin.today", return_value="2026-10-10")
	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=RuntimeError)
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_started_term_requires_campus_confirmation(self, _translate, _throw, _today):
		db = Mock()
		db.exists.return_value = None
		db.get_value.return_value = "2026-10-05"
		with patch("qas_custom.services.school_admin.frappe.db", new=db), self.assertRaises(RuntimeError):
			_validate_weekly_timeslot_change(slot(), {"campus": "Upper Mount Gravatt"})

		with patch("qas_custom.services.school_admin.frappe.db", new=db):
			_validate_weekly_timeslot_change(
				slot(),
				{"campus": "Upper Mount Gravatt", "confirm_active_term_campus_change": 1},
			)
