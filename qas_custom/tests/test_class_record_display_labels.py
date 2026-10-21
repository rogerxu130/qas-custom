import json
from pathlib import Path
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


class TestScheduleLabelMigration(TestCase):
	def test_label_columns_can_store_full_labels_beyond_140_characters(self):
		from qas_custom.services.class_record_labels import refresh_linked_course_session_labels
		root = Path(__file__).resolve().parents[1] / "qas_custom" / "doctype"
		for doctype in ("course_sessions", "weekly_timeslot"):
			with self.subTest(doctype=doctype):
				schema = json.loads((root / doctype / (doctype + ".json")).read_text())
				field = next(f for f in schema["fields"] if f["fieldname"] == "display_label")
				self.assertEqual(field["fieldtype"], "Small Text")
		long_course = "Storytelling and Art - Beginner " * 6
		timeslot = frappe._dict(course=long_course, teacher="Teacher", class_language="English")
		session = frappe._dict(weekly_timeslot="CLASS", session_date="2026-10-25")
		db = Mock()
		db.get_value.side_effect = lambda dt, name, fields, **kw: timeslot if dt == "Weekly Timeslot" else name
		with patch("qas_custom.services.class_record_labels.frappe.db", db), patch(
			"qas_custom.services.class_record_labels.frappe.get_all", return_value=["SESSION"]
		), patch("qas_custom.services.class_record_labels.frappe.get_doc", return_value=session):
			refresh_linked_course_session_labels(frappe._dict(name="CLASS"))
		label = db.set_value.call_args.args[3]
		self.assertGreater(len(label), 140)
		self.assertIn(long_course, label)

	def test_migration_expands_label_columns_before_refresh(self):
		from qas_custom.patches.v2026_10_21_session_schedule_times import execute
		calls = Mock()
		with patch("frappe.reload_doc", calls.reload), patch("frappe.db", calls.db), patch(
			"frappe.get_all", return_value=["CLASS"]
		), patch("qas_custom.services.class_record_labels.refresh_linked_course_session_labels", calls.refresh):
			execute()
		self.assertEqual(calls.mock_calls[0].args, ("qas_custom", "doctype", "course_sessions"))
		self.assertEqual(calls.mock_calls[1].args, ("qas_custom", "doctype", "weekly_timeslot"))
		self.assertEqual(calls.mock_calls[-1].args, (frappe._dict(name="CLASS"),))
