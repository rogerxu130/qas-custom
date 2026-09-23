"""Behavior contract for shared course-session booking resources."""

import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.modules.course_schedule import session_resources as resources


class TestCourseSessionResources(TestCase):
	def test_active_rows_returns_locked_query_results_unchanged(self):
		rows = [object()]
		db = SimpleNamespace(sql=Mock(return_value=rows))
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertIs(resources.active_rows("CS-1", exclude="ATT-1", lock=True), rows)
		db.sql.assert_called_once()
		query, parameters = db.sql.call_args.args
		normalized_query = " ".join(query.lower().split())
		projection = re.search(r"\bselect\b(.*?)\bfrom\b", normalized_query)
		self.assertIsNotNone(projection)
		for field in ("name", "student", "enrollment_type", "source_doctype", "source_document", "makeup_voucher"):
			with self.subTest(field=field):
				self.assertRegex(projection.group(1), rf"\b(?:\w+\s*\.\s*)?{field}\b")
		self.assertIn("from `tabclass attendance entry`", normalized_query)
		self.assertRegex(normalized_query, r"\b(?:\w+\s*\.\s*)?course_session\s*=\s*%s\b")
		self.assertRegex(normalized_query, r"\b(?:\w+\s*\.\s*)?status\s+not\s+in\s*\(\s*'cancelled'\s*,\s*'leave'\s*\)")
		self.assertRegex(normalized_query, r"\b(?:\w+\s*\.\s*)?name\s*!=\s*%s\b")
		self.assertTrue(normalized_query.endswith("for update"))
		self.assertEqual(parameters, ("CS-1", "ATT-1"))
		self.assertEqual(db.sql.call_args.kwargs, {"as_dict": True})

	def test_active_rows_default_exclusion_does_not_lock(self):
		rows = [object()]
		db = SimpleNamespace(sql=Mock(return_value=rows))
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertIs(resources.active_rows("CS-2", lock=False), rows)
		db.sql.assert_called_once()
		query, parameters = db.sql.call_args.args
		normalized_query = " ".join(query.lower().split())
		self.assertNotIn("for update", normalized_query)
		self.assertEqual(parameters, ("CS-2", ""))
		self.assertEqual(db.sql.call_args.kwargs, {"as_dict": True})

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
						resources.student_has_conflict("STU-1", session, slot, exclude="ATT-1"),
						expected,
					)
				query, parameters = db.sql.call_args.args
				normalized_query = " ".join(query.lower().split())
				self.assertNotIn("for update", normalized_query)
				self.assertIn("a.status not in ('cancelled', 'leave')", normalized_query)
				self.assertIn("s.status != 'cancelled'", normalized_query)
				self.assertIn("s.session_date=%s", normalized_query)
				self.assertEqual(parameters, ("STU-1", "2026-09-23", "ATT-1"))
				self.assertEqual(db.sql.call_args.kwargs, {"as_dict": True})

	def test_locked_conflict_reads_only_attendance_under_lock_then_checks_times(self):
		session = frappe._dict(name="CS-1", session_date="2026-09-23")
		slot = frappe._dict(start_time="10:00", end_time="11:00")
		for other_start, other_end, expected in (("10:30", "11:30", True), ("11:00", "12:00", False)):
			with self.subTest(other_start=other_start):
				# The newly committed attendance ID appears in the locking read.
				db = SimpleNamespace(sql=Mock(side_effect=[
					[frappe._dict(name="ATT-NEW", course_session="CS-2")],
					[frappe._dict(course_session="CS-2", start_time=other_start, end_time=other_end)],
				]))
				with patch.object(resources, "frappe", SimpleNamespace(db=db)):
					self.assertEqual(resources.student_has_conflict("STU-1", session, slot, exclude="ATT-OLD", lock=True), expected)
				self.assertEqual(db.sql.call_count, 2)
				locked, times = db.sql.call_args_list
				self.assertIn("FOR UPDATE", locked.args[0])
				self.assertIn("`tabClass Attendance Entry`", locked.args[0])
				self.assertNotIn("JOIN", locked.args[0])
				self.assertNotIn("`tabCourse Sessions`", locked.args[0])
				self.assertNotIn("`tabWeekly Timeslot`", locked.args[0])
				self.assertIn("status NOT IN ('Cancelled', 'Leave')", locked.args[0])
				self.assertEqual(locked.args[1], ("STU-1", "ATT-OLD"))
				self.assertNotIn("FOR UPDATE", times.args[0])
				self.assertIn("s.status != 'Cancelled'", times.args[0])
				self.assertIn("s.session_date=%s", times.args[0])
				self.assertEqual(times.args[1], (("CS-2",), "2026-09-23"))

	def test_locked_conflict_same_session_is_duplicate_even_if_times_differ(self):
		db = SimpleNamespace(sql=Mock(side_effect=[
			[frappe._dict(name="ATT-NEW", course_session="CS-1")],
			[frappe._dict(course_session="CS-1", start_time="12:00", end_time="13:00")],
		]))
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertTrue(resources.student_has_conflict(
				"STU-1", frappe._dict(name="CS-1", session_date="2026-09-23"),
				frappe._dict(start_time="10:00", end_time="11:00"), lock=True,
			))

	def test_locked_conflict_with_no_attendance_skips_schedule_read(self):
		db = SimpleNamespace(sql=Mock(return_value=[]))
		with patch.object(resources, "frappe", SimpleNamespace(db=db)):
			self.assertFalse(resources.student_has_conflict("STU-1", frappe._dict(session_date="2026-09-23"), frappe._dict(), lock=True))
		db.sql.assert_called_once()

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
