from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.makeup.commands import _get_leave_session, _queue_leave_requested_notification
from qas_custom.services.school_admin import _build_school_admin_leave_session_options


class TestSchoolAdminRetroactiveLeave(TestCase):
	def test_options_only_include_eligible_current_active_term_rows(self):
		attendance_rows = [
			self._attendance("ATT-PAST", "CS-PAST", "Absent"),
			self._attendance("ATT-FUTURE", "CS-FUTURE", "To be started"),
			self._attendance("ATT-PRESENT", "CS-PRESENT", "Present"),
		]
		sessions = [
			self._session("CS-PAST", "2026-07-08"),
			self._session("CS-FUTURE", "2026-07-22"),
			self._session("CS-PRESENT", "2026-07-09"),
		]
		rows = _build_school_admin_leave_session_options(
			attendance_rows=attendance_rows,
			enrollment_rows=[{
				"name": "ENR-001", "student": "STU-001", "term": "TERM-ACTIVE",
				"weekly_timeslot": "WT-001", "enrollment_type": "Full-Term", "status": "Active",
			}],
			term_rows=[{
				"name": "TERM-ACTIVE", "term_name": "Term 3 2026", "start_date": "2026-07-01",
				"end_date": "2026-09-30", "status": "Active",
			}],
			session_rows=sessions,
			timeslot_map={"WT-001": self._timeslot()},
			current_datetime=datetime(2026, 7, 15, 12, 0),
		)

		self.assertEqual([row["session_id"] for row in rows], ["CS-PAST", "CS-FUTURE"])
		self.assertTrue(rows[0]["is_past"])
		self.assertFalse(rows[1]["is_past"])

	def test_inactive_term_is_excluded(self):
		rows = _build_school_admin_leave_session_options(
			attendance_rows=[self._attendance("ATT-PAST", "CS-PAST", "Absent")],
			enrollment_rows=[{
				"name": "ENR-001", "student": "STU-001", "term": "TERM-OLD",
				"weekly_timeslot": "WT-001", "enrollment_type": "Full-Term", "status": "Active",
			}],
			term_rows=[{
				"name": "TERM-OLD", "term_name": "Term 2 2026", "start_date": "2026-04-01",
				"end_date": "2026-06-30", "status": "Completed",
			}],
			session_rows=[self._session("CS-PAST", "2026-06-15")],
			timeslot_map={"WT-001": self._timeslot()},
			current_datetime=datetime(2026, 7, 15, 12, 0),
		)
		self.assertEqual(rows, [])

	def test_admin_policy_accepts_exact_absent_past_attendance(self):
		fake_frappe = self._fake_frappe("Absent")
		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe), patch(
			"qas_custom.modules.makeup.commands.now_datetime", return_value=datetime(2026, 7, 15, 12, 0),
		):
			_session, attendance, _timeslot = _get_leave_session(
				"STU-001", "CS-PAST", allowed_attendance_statuses=("To be started", "Absent"),
				allow_started_session=True, attendance_entry="ATT-001",
			)
		self.assertEqual(attendance.status, "Absent")
		self.assertEqual(fake_frappe.db.get_value.call_args.args[1]["name"], "ATT-001")

	def test_parent_default_rejects_absent_session(self):
		fake_frappe = self._fake_frappe("Absent", rejected=True)
		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "not eligible"):
				_get_leave_session("STU-001", "CS-PAST")

	@patch("qas_custom.modules.makeup.commands.enqueue_session_staff_notification")
	def test_retroactive_leave_skips_staff_notification(self, mock_enqueue):
		result = _queue_leave_requested_notification(
			notify_staff=False, course_session="CS-PAST", student="STU-001", leave_request="LR-001",
		)
		self.assertEqual(result, {"skipped": True, "reason": "retroactive_school_admin_leave"})
		mock_enqueue.assert_not_called()

	@staticmethod
	def _attendance(name, session, status):
		return {
			"name": name, "student": "STU-001", "course_session": session, "status": status,
			"enrollment_type": "Full-Term", "source_doctype": "Enrollment", "source_document": "ENR-001",
		}

	@staticmethod
	def _session(name, date):
		return {"name": name, "session_date": date, "weekly_timeslot": "WT-001", "status": "Scheduled"}

	@staticmethod
	def _timeslot():
		return {
			"course": "Creative Art", "campus": "Upper Mount Gravatt", "classroom": "Room 3",
			"teacher": "Teacher One", "day_of_week": "Wednesday", "start_time": "16:00:00",
			"end_time": "17:30:00",
		}

	@staticmethod
	def _fake_frappe(status, rejected=False):
		return SimpleNamespace(
			get_doc=Mock(return_value=SimpleNamespace(
				name="CS-PAST", weekly_timeslot="WT-001", session_date="2026-07-08",
			)),
			db=SimpleNamespace(get_value=Mock(return_value=SimpleNamespace(name="ATT-001", status=status))),
			get_cached_doc=Mock(return_value=SimpleNamespace(course="Creative Art", start_time="16:00:00")),
			throw=Mock(side_effect=RuntimeError("not eligible") if rejected else None),
		)
