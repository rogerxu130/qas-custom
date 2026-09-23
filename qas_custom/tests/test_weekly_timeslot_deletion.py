from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import weekly_timeslot_deletion as service


class TestWeeklyTimeslotDeletion(TestCase):
	def setUp(self):
		flags = patch.object(frappe.local, "flags", frappe._dict(in_test=False), create=True)
		flags.start()
		self.addCleanup(flags.stop)
		self.doc = frappe._dict(doctype="Weekly Timeslot", name="W1", modified="v1",
			start_time="16:00:00", display_label="Saved class")
		self.sessions = [frappe._dict(name="C1", session_date="2026-10-09", status="Scheduled", modified="v1")]
		self.records = {"Enrollment": [], "Inquiry": [], "Class Attendance Entry": []}
		def get_all(dt, **kwargs):
			if dt == "Course Sessions":
				return [s.name for s in self.sessions] if kwargs.get("pluck") else self.sessions
			return self.records[dt]
		def throw(message):
			raise ValueError(message)
		self.fake = SimpleNamespace(get_doc=lambda dt, name: self.doc if dt == "Weekly Timeslot" else frappe._dict(doctype=dt, name=name),
			get_all=Mock(side_effect=get_all), throw=throw, db=Mock(), delete_doc=Mock())
		for name, value in [("frappe", self.fake), ("_", lambda s: s),
			("_require_school_admin", Mock()), ("now_datetime", lambda: datetime(2026, 9, 23)),
			("get_linked_docs", Mock(return_value=[])), ("get_dynamic_linked_docs", Mock(return_value=[]))]:
			p = patch.object(service, name, value)
			p.start()
			self.addCleanup(p.stop)

	def test_empty_future_class_deletes_children_then_parent_without_force(self):
		preview = service.preview("W1")
		self.assertEqual(preview["session_count"], 1)
		self.assertEqual(preview["blocking_errors"], [])
		result = service.execute("W1", preview["confirmation_token"])
		self.assertEqual(result["deleted_session_count"], 1)
		self.assertEqual([c.args for c in self.fake.delete_doc.call_args_list], [("Course Sessions", "C1"), ("Weekly Timeslot", "W1")])
		self.assertTrue(all(c.kwargs == dict(ignore_permissions=True, ignore_missing=False) for c in self.fake.delete_doc.call_args_list))

	def test_class_with_no_sessions(self):
		self.sessions.clear()
		preview = service.preview("W1")
		self.assertEqual(service.execute("W1", preview["confirmation_token"])["deleted_session_count"], 0)

	def test_each_student_link_blocks_even_after_preview(self):
		for dt in self.records:
			with self.subTest(dt=dt):
				token = service.preview("W1")["confirmation_token"]
				self.records[dt] = ["LINK-1"]
				with self.assertRaisesRegex(ValueError, "Linked"):
					service.execute("W1", token)
				self.fake.delete_doc.assert_not_called()
				self.records[dt] = []

	def test_started_completed_and_invalid_sessions_block(self):
		for date, status in [("2026-09-22", "Scheduled"), ("2026-10-09", "Completed"), (None, "Scheduled"), ("2026-10-09", "Unknown")]:
			with self.subTest(date=date, status=status):
				self.sessions[0].update(session_date=date, status=status)
				self.assertTrue(service.preview("W1")["blocking_errors"])

	def test_today_before_and_at_start_boundary(self):
		self.sessions[0].session_date = "2026-09-23"
		with patch.object(service, "now_datetime", return_value=datetime(2026, 9, 23, 15, 59)):
			self.assertFalse(service.preview("W1")["blocking_errors"])
		with patch.object(service, "now_datetime", return_value=datetime(2026, 9, 23, 16)):
			self.assertTrue(service.preview("W1")["blocking_errors"])

	def test_static_and_dynamic_links_block_but_own_sessions_do_not(self):
		service.get_linked_docs.side_effect = lambda doc: [dict(reference_doctype="Course Sessions", reference_docname="C1")] if doc.doctype == "Weekly Timeslot" else []
		self.assertFalse(service.preview("W1")["blocking_errors"])
		service.get_dynamic_linked_docs.return_value = [dict(reference_doctype="Sales Invoice", reference_docname="INV1")]
		self.assertIn("Linked Sales Invoice: INV1", service.preview("W1")["blocking_errors"])

	def test_changed_state_requires_new_preview(self):
		token = service.preview("W1")["confirmation_token"]
		self.sessions[0].modified = "v2"
		with self.assertRaisesRegex(ValueError, "changed after preview"):
			service.execute("W1", token)
		self.fake.delete_doc.assert_not_called()

	def test_partial_failure_rolls_back(self):
		token = service.preview("W1")["confirmation_token"]
		self.fake.delete_doc.side_effect = [None, RuntimeError("new link")]
		with self.assertRaisesRegex(RuntimeError, "new link"):
			service.execute("W1", token)
		self.fake.db.rollback.assert_called_once()

	def test_permission_checked_before_queries_and_deletion(self):
		service._require_school_admin.side_effect = PermissionError()
		for fn, args in [(service.preview, ("W1",)), (service.execute, ("W1", "token"))]:
			with self.assertRaises(PermissionError):
				fn(*args)
		self.fake.get_all.assert_not_called()
		self.fake.delete_doc.assert_not_called()

	def test_confirmation_required(self):
		with self.assertRaisesRegex(ValueError, "Preview and confirm"):
			service.execute("W1")
		self.fake.delete_doc.assert_not_called()
