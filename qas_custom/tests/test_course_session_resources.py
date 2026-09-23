"""Behavior contract for shared course-session booking resources."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.modules.course_schedule import session_resources as resources


class TestCourseSessionResources(TestCase):
	def test_adjacent_classes_do_not_overlap_for_supported_time_values(self):
		for start, end, overlap_start, next_end in (
			("10:00", "11:00", "10:30", "12:00"),
			(time(10), time(11), time(10, 30), time(12)),
			(timedelta(hours=10), timedelta(hours=11), timedelta(hours=10, minutes=30), timedelta(hours=12)),
		):
			with self.subTest(start=start):
				self.assertFalse(resources.overlaps(start, end, end, next_end))
				self.assertFalse(resources.overlaps(end, next_end, start, end))
				self.assertTrue(resources.overlaps(start, end, overlap_start, next_end))
				self.assertTrue(resources.overlaps(overlap_start, next_end, start, end))
				self.assertTrue(resources.overlaps(start, next_end, overlap_start, end))

	def test_only_scheduled_sessions_after_the_start_boundary_are_future(self):
		session = frappe._dict(status="Scheduled", session_date="2026-09-23")
		with patch.object(resources, "now_datetime", return_value=datetime(2026, 9, 23, 10, 0)):
			self.assertFalse(resources.session_is_future(session, frappe._dict(start_time="10:00")))
			self.assertTrue(resources.session_is_future(session, frappe._dict(start_time="10:01")))
			session.session_date = "2026-09-22"
			self.assertFalse(resources.session_is_future(session, frappe._dict(start_time="10:01")))
			session.session_date = "2026-09-24"
			self.assertTrue(resources.session_is_future(session, frappe._dict(start_time="09:00")))
			session.session_date = "2026-09-23"
			for status in ("Completed", "Cancelled"):
				with self.subTest(status=status):
					session.status = status
					self.assertFalse(resources.session_is_future(session, frappe._dict(start_time="10:01")))

	def test_missing_classroom_has_zero_capacity_without_database_lookup(self):
		db = SimpleNamespace(get_value=Mock())
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertEqual(resources.classroom_capacity(frappe._dict(classroom=None), lock=True), 0)
		db.get_value.assert_not_called()

	def test_classroom_capacity_reads_capacity_with_requested_lock(self):
		db = SimpleNamespace(get_value=Mock(return_value="12"))
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertEqual(resources.classroom_capacity(frappe._dict(classroom="ROOM-1"), lock=True), 12)
		db.get_value.assert_called_once_with("Classroom", "ROOM-1", "capacity", for_update=True)

	def test_student_conflicts_include_same_session_and_overlap_but_not_adjacent_session(self):
		session = frappe._dict(name="CS-1", session_date="2026-09-23")
		slot = frappe._dict(start_time="10:00", end_time="11:00")
		cases = (
			("same session", frappe._dict(course_session="CS-1", start_time="12:00", end_time="13:00"), True),
			("overlap", frappe._dict(course_session="CS-2", start_time="10:30", end_time="11:30"), True),
			("adjacent", frappe._dict(course_session="CS-2", start_time="11:00", end_time="12:00"), False),
		)
		for label, row, expected in cases:
			with self.subTest(case=label):
				db = SimpleNamespace(sql=Mock(return_value=[row]))
				with patch.object(resources, "frappe", SimpleNamespace(db=db)):
					self.assertEqual(
						resources.student_has_conflict("STU-1", session, slot, exclude="ATT-1", lock=True),
						expected,
					)
				query, parameters = db.sql.call_args.args
				normalized_query = " ".join(query.lower().split())
				self.assertIn("for update", normalized_query)
				self.assertIn("a.status not in ('cancelled', 'leave')", normalized_query)
				self.assertIn("s.status != 'cancelled'", normalized_query)
				self.assertIn("s.session_date=%s", normalized_query)
				self.assertEqual(parameters, ("STU-1", "2026-09-23", "ATT-1"))
				self.assertEqual(db.sql.call_args.kwargs, {"as_dict": True})

	def test_session_context_fetches_session_then_timeslot_with_lock(self):
		session = frappe._dict(name="CS-1", weekly_timeslot="WT-1")
		slot = frappe._dict(name="WT-1")
		get_doc = Mock(side_effect=[session, slot])
		with patch.object(resources, "frappe", SimpleNamespace(get_doc=get_doc)):
			self.assertEqual(resources.session_context("CS-1", lock=True), (session, slot))
		get_doc.assert_has_calls(
			[
				call("Course Sessions", "CS-1", for_update=True),
				call("Weekly Timeslot", "WT-1", for_update=True),
			]
		)
		self.assertEqual(get_doc.call_count, 2)
