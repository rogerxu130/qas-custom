from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import _validate_exact_weekly_timeslot_duplicate


def slot(**updates):
	values = dict(
		name="WT-NEW", term="Term 4 2026", course="Course A", class_language="English",
		campus="Indooroopilly", classroom="R4", day_of_week="Saturday",
		start_time="14:00:00", end_time="15:30:00", status="Active", teacher="Jayson",
	)
	values.update(updates)
	return frappe._dict(values)


class TestWeeklyTimeslotDuplicateValidation(TestCase):
	@patch("qas_custom.services.school_admin.frappe.throw", side_effect=RuntimeError)
	@patch("qas_custom.services.school_admin._", side_effect=lambda value: value)
	def test_exact_active_duplicate_is_rejected(self, translate, throw):
		db = Mock()
		db.exists.return_value = "WT-EXISTING"
		with patch("qas_custom.services.school_admin.frappe.db", new=db), self.assertRaises(RuntimeError):
			_validate_exact_weekly_timeslot_duplicate(slot())
		filters = db.exists.call_args.args[1]
		self.assertNotIn("teacher", filters)
		self.assertEqual(filters["class_language"], "English")

	def test_current_record_is_allowed_during_update(self):
		db = Mock()
		db.exists.return_value = "WT-NEW"
		with patch("qas_custom.services.school_admin.frappe.db", new=db):
			_validate_exact_weekly_timeslot_duplicate(slot())

	def test_inactive_record_skips_duplicate_check(self):
		db = Mock()
		with patch("qas_custom.services.school_admin.frappe.db", new=db):
			_validate_exact_weekly_timeslot_duplicate(slot(status="Inactive"))
		db.exists.assert_not_called()
