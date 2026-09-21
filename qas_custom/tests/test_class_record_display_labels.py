from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.class_record_labels import build_course_session_label, build_weekly_timeslot_label


LABELS = {
	("Course", "Realistic Art - Beginner", "course_name"): "Realistic Art - Beginner",
	("Campus", "Indooroopilly", "campus_name"): "Indooroopilly",
	("Classroom", "indooroopilly-r4", "classroom_name"): "R4",
	("Teacher", "Jayson", "teacher_name"): "Jayson",
	("Teacher", "Julie", "teacher_name"): "Julie",
	("Term", "Term 4 2026", "term_name"): "Term 4 2026",
}


class TestClassRecordDisplayLabels(TestCase):
	def test_weekly_timeslot_label_uses_business_fields_not_id(self):
		db = Mock()
		db.get_value.side_effect = lambda doctype, name, field, **kwargs: LABELS.get((doctype, name, field))
		doc = frappe._dict(
			term="Term 4 2026", course="Realistic Art - Beginner", class_language="English",
			campus="Indooroopilly", classroom="indooroopilly-r4", teacher="Jayson",
			day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00",
		)
		with patch("qas_custom.services.class_record_labels.frappe.db", new=db):
			self.assertEqual(
				build_weekly_timeslot_label(doc),
				"Realistic Art - Beginner · English · Indooroopilly R4 · Sat · 14:00-15:30 · Jayson · Term 4 2026",
			)

	def test_course_session_label_prefers_teacher_override(self):
		db = Mock()
		def value(doctype, name, field, **kwargs):
			if doctype == "Weekly Timeslot":
				return frappe._dict(
					term="Term 4 2026", course="Realistic Art - Beginner", class_language="English",
					campus="Indooroopilly", classroom="indooroopilly-r4", teacher="Jayson",
					day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00",
				)
			return LABELS.get((doctype, name, field))
		db.get_value.side_effect = value
		doc = frappe._dict(weekly_timeslot="WTS-2026-00009", session_date="2026-10-10", teacher_override="Julie")
		with patch("qas_custom.services.class_record_labels.frappe.db", new=db):
			label = build_course_session_label(doc)
		self.assertIn("10 Oct 2026", label)
		self.assertIn("Realistic Art - Beginner", label)
		self.assertIn("Indooroopilly R4", label)
		self.assertIn("Julie", label)
		self.assertNotIn("WTS-2026-00009", label)
