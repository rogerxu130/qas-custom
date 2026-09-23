from unittest import TestCase
from unittest.mock import Mock, patch
from contextlib import ExitStack
from datetime import datetime

import frappe

from qas_custom.services.weekly_timeslot_course_change import _intended_payload, _preview, _session_has_started, _teacher_conflict, execute


class TestSchoolAdminWeeklyTimeslotCourseChange(TestCase):
	def setUp(self):
		clock = patch("qas_custom.services.weekly_timeslot_course_change.now_datetime", return_value=datetime(2026, 9, 21, 10, 0))
		clock.start()
		self.addCleanup(clock.stop)

	def test_start_boundary_uses_saved_time_and_completed_status(self):
		current = datetime(2026, 9, 21, 10, 0)
		for date, time, status, expected in [
			("2026-09-21", "10:01", "Scheduled", False),
			("2026-09-21", "10:00", "Scheduled", True),
			("2026-09-21", "09:00", "Scheduled", True),
			("2026-09-20", "11:00", "Scheduled", True),
			("2026-09-22", "09:00", "Completed", True),
		]:
			with self.subTest(date=date, time=time, status=status):
				self.assertEqual(_session_has_started({"session_date": date, "status": status}, time, current), expected)

	def test_teacher_preview_allows_active_term_but_blocks_started_or_attended_sessions(self):
		module = "qas_custom.services.weekly_timeslot_course_change."
		for date, attended, blocked in [("2026-09-22", False, False), ("2026-09-21", False, True), ("2026-09-22", True, True)]:
			with self.subTest(date=date, attended=attended), ExitStack() as stack:
				doc = frappe._dict(name="WTS-1", term="Term", course="Course A", teacher="Old", start_time="09:00", end_time="10:00")
				session = frappe._dict(name="CS-1", session_date=date, status="Scheduled", teacher_override="Substitute")
				db = Mock()
				db.exists.side_effect = lambda dt, filters=None: True if dt == "Course" else attended
				db.get_value.side_effect = [frappe._dict(status="Active"), 60, 60]
				for name, kwargs in {
					"frappe.db": {"new": db}, "frappe.get_doc": {"return_value": doc},
					"frappe.get_all": {"side_effect": [[session], [], []]},
					"today": {"return_value": "2026-09-21"}, "_": {"side_effect": lambda text: text},
					"_validate_weekly_timeslot_change": {}, "_apply_weekly_timeslot_payload": {},
				}.items():
					stack.enter_context(patch(module + name, **kwargs))
				report = _preview("WTS-1", "Course A", {"teacher": ""}, "2026-10-05")
				self.assertEqual(bool(report["blocking_errors"]), blocked)
				self.assertEqual(report["effective_date"], "2026-09-21")
				self.assertEqual(report["teacher_change_policy"], "all_unstarted_sessions")
				db.set_value.assert_not_called()

	def test_teacher_only_execute_clears_overrides_without_rewriting_enrollments(self):
		module = "qas_custom.services.weekly_timeslot_course_change."
		report = {"blocking_errors": [], "confirmation_token": "token", "effective_date": "2026-09-21",
			"intended_payload": {"teacher": ""}, "changes": {"teacher": {}},
			"sessions": [{"name": "CS-1", "teacher_override": "Substitute"}, {"name": "CS-2", "teacher_override": ""}],
			"session_date_mapping": [], "enrollments": [{"name": "ENR-1"}], "old_course": "Course A"}
		with ExitStack() as stack:
			db = Mock()
			for name, kwargs in {"_preview": {"return_value": report}, "_require_school_admin": {},
				"_apply_weekly_timeslot_payload": {}, "_add_comment": {}, "refresh_linked_course_session_labels": {},
				"frappe.get_doc": {"return_value": Mock()}, "frappe.db": {"new": db},
				"_": {"side_effect": lambda text: text}}.items():
				stack.enter_context(patch(module + name, **kwargs))
			result = execute.__wrapped__("WTS-1", "Course A", "token", {"teacher": ""})
			db.set_value.assert_called_once_with("Course Sessions", "CS-1", "teacher_override", "", update_modified=True)
			db.commit.assert_called_once()
			self.assertEqual(result["updated_enrollment_count"], 0)

	def test_execute_rechecks_started_sessions_and_rejects_stale_confirmation_before_writing(self):
		module = "qas_custom.services.weekly_timeslot_course_change."
		for errors, token in [(["Use Change weekly teacher"], "token"), ([], "changed-token")]:
			with self.subTest(errors=errors, token=token), ExitStack() as stack:
				stack.enter_context(patch(module + "_require_school_admin"))
				stack.enter_context(patch(module + "_preview", return_value={"blocking_errors": errors, "confirmation_token": token}))
				stack.enter_context(patch(module + "frappe.throw", side_effect=RuntimeError))
				stack.enter_context(patch(module + "_", side_effect=lambda text: text))
				get_doc = stack.enter_context(patch(module + "frappe.get_doc"))
				with self.assertRaises(RuntimeError):
					execute.__wrapped__("WTS-1", "Course A", "token", {"teacher": "New"})
				get_doc.assert_not_called()

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
		), patch("qas_custom.services.weekly_timeslot_course_change._validate_weekly_timeslot_change"), patch(
			"qas_custom.services.weekly_timeslot_course_change._apply_weekly_timeslot_payload"
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

	def test_intended_payload_preserves_explicit_blank_teacher_and_rejects_unknown_fields(self):
		doc = frappe._dict(term="Term 4 2026", course="Course A", teacher="Teacher A", start_time="09:00:00", end_time="10:30:00")
		result = _intended_payload(doc, "Course B", {"course": "Course B", "teacher": "", "start_time": "09:00"})
		self.assertEqual(result["course"], "Course B")
		self.assertEqual(result["teacher"], "")
		self.assertEqual(result["start_time"], "09:00")
		with patch("qas_custom.services.weekly_timeslot_course_change.frappe.throw", side_effect=RuntimeError), patch(
			"qas_custom.services.weekly_timeslot_course_change._", side_effect=lambda value: value
		):
			with self.assertRaises(RuntimeError):
				_intended_payload(doc, "Course B", {"unexpected": "value"})

	def test_teacher_overlap_is_reported_for_complete_target(self):
		doc = frappe._dict(name="WTS-1", term="Term 4 2026")
		other = frappe._dict(name="WTS-2", course="Other Class", start_time="09:30:00", end_time="11:00:00")
		intended = {
			"teacher": "Teacher A", "status": "Active", "day_of_week": "Saturday",
			"start_time": "09:00:00", "end_time": "10:30:00",
		}
		with patch("qas_custom.services.weekly_timeslot_course_change.frappe.get_all", return_value=[other]), patch(
			"qas_custom.services.weekly_timeslot_course_change._", side_effect=lambda value: value
		):
			self.assertIn("Teacher A", _teacher_conflict(doc, intended))
		intended["teacher"] = ""
		self.assertIsNone(_teacher_conflict(doc, intended))

	@patch("qas_custom.services.weekly_timeslot_course_change._require_school_admin")
	@patch("qas_custom.services.weekly_timeslot_course_change.refresh_linked_course_session_labels")
	@patch("qas_custom.services.weekly_timeslot_course_change._add_comment")
	@patch("qas_custom.services.weekly_timeslot_course_change._apply_weekly_timeslot_payload")
	@patch("qas_custom.services.weekly_timeslot_course_change._preview")
	def test_execute_applies_complete_payload_and_commits_once(self, preview, apply_payload, _comment, refresh, _require):
		report = {
			"blocking_errors": [], "confirmation_token": "token", "effective_date": "2026-10-05",
			"intended_payload": {"course": "Course B", "teacher": "", "start_time": "09:00", "end_time": "10:00"},
			"changes": {"course": {}, "teacher": {}, "end_time": {}}, "sessions": [],
			"session_date_mapping": [], "enrollments": [{"name": "ENR-1"}],
			"old_course": "Course A", "new_course": "Course B",
		}
		preview.return_value = report
		doc = Mock()
		doc.name = "WTS-1"
		doc.get.return_value = "Teacher A"
		db = Mock()
		with patch("qas_custom.services.weekly_timeslot_course_change.frappe.get_doc", return_value=doc), patch(
			"qas_custom.services.weekly_timeslot_course_change.frappe.db", new=db
		), patch(
			"qas_custom.services.weekly_timeslot_course_change._", side_effect=lambda value: value
		):
			result = execute.__wrapped__("WTS-1", "Course B", "token", {"course": "Course B", "teacher": ""}, "2026-10-05")
		apply_payload.assert_called_once_with(doc, report["intended_payload"])
		doc.save.assert_called_once_with(ignore_permissions=True)
		db.set_value.assert_called_once_with("Enrollment", "ENR-1", "course", "Course B", update_modified=True)
		db.commit.assert_called_once()
		db.rollback.assert_not_called()
		refresh.assert_called_once_with(doc)
		self.assertTrue(result["completed"])

	@patch("qas_custom.services.weekly_timeslot_course_change._require_school_admin")
	@patch("qas_custom.services.weekly_timeslot_course_change._apply_weekly_timeslot_payload")
	@patch("qas_custom.services.weekly_timeslot_course_change._preview")
	def test_execute_rolls_back_complete_operation_on_failure(self, preview, _apply, _require):
		preview.return_value = {
			"blocking_errors": [], "confirmation_token": "token", "effective_date": "",
			"intended_payload": {"course": "Course B"}, "changes": {"course": {}},
			"sessions": [], "session_date_mapping": [], "enrollments": [],
			"old_course": "Course A", "new_course": "Course B",
		}
		doc = Mock()
		doc.name = "WTS-1"
		doc.get.return_value = ""
		doc.save.side_effect = RuntimeError("save failed")
		db = Mock()
		with patch("qas_custom.services.weekly_timeslot_course_change.frappe.get_doc", return_value=doc), patch(
			"qas_custom.services.weekly_timeslot_course_change.frappe.db", new=db
		), self.assertRaisesRegex(RuntimeError, "save failed"):
			execute.__wrapped__("WTS-1", "Course B", "token")
		db.rollback.assert_called_once()
		db.commit.assert_not_called()
