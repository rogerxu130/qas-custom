from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.makeup.commands import (
	_ensure_leave_makeup_voucher,
	complete_parent_leave_and_keep_voucher_core,
	complete_parent_leave_and_redeem_core,
	get_parent_leave_makeup_options_core,
)


class FakeDoc:
	def __init__(self, **values):
		self.__dict__.update(values)
		self.flags = values.get("flags") or SimpleNamespace()
		self.meta = values.get("meta") or SimpleNamespace(has_field=lambda _fieldname: True)
		self.inserted = False

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def insert(self, ignore_permissions=False):
		self.inserted = ignore_permissions

	def save(self, ignore_permissions=False):
		return None


class TestParentLeaveMakeupChoice(TestCase):
	def test_preview_returns_options_without_creating_leave_or_voucher(self):
		session = SimpleNamespace(name="CS-LEAVE", session_date="2026-08-01")
		timeslot = SimpleNamespace(
			course="Anime",
			day_of_week="Saturday",
			start_time="10:00:00",
			end_time="11:30:00",
			campus="Indooroopilly",
			classroom="Room 1",
		)
		with patch(
			"qas_custom.modules.makeup.commands._get_leave_session",
			return_value=(session, SimpleNamespace(name="ATT-LEAVE"), timeslot),
		), patch(
			"qas_custom.modules.makeup.commands._validate_no_active_leave"
		) as validate_leave, patch(
			"qas_custom.modules.makeup.commands._get_redeemable_makeup_sessions",
			return_value=[{"session_id": "CS-MAKEUP"}],
		) as get_sessions, patch(
			"qas_custom.modules.makeup.commands.frappe.new_doc"
		) as new_doc:
			result = get_parent_leave_makeup_options_core(
				parent=SimpleNamespace(name="PAR-001"),
				students=[{"name": "STU-LEAVE", "student_name": "Ava"}],
				student="STU-LEAVE",
				course_session="CS-LEAVE",
			)

		self.assertEqual(result["source_session"]["session_id"], "CS-LEAVE")
		self.assertEqual(result["selected_redeem_student"], "STU-LEAVE")
		self.assertEqual(result["available_sessions"], [{"session_id": "CS-MAKEUP"}])
		self.assertEqual(get_sessions.call_args.kwargs["excluded_session_ids"], {"CS-LEAVE"})
		validate_leave.assert_called_once_with(student="STU-LEAVE", course_session="CS-LEAVE")
		new_doc.assert_not_called()

	def test_book_now_creates_leave_only_after_target_is_previewed(self):
		options = {
			"selected_redeem_student": "STU-MAKEUP",
			"available_sessions": [{"session_id": "CS-MAKEUP"}],
		}
		leave_result = {
			"leave_request": "LR-001",
			"makeup_voucher": "MV-001",
			"session": {"session_id": "CS-LEAVE"},
		}
		booking_result = {
			"voucher": {"voucher_id": "MV-001", "status": "Used"},
			"attendance_entry": "ATT-MAKEUP",
			"session": {"session_id": "CS-MAKEUP"},
			"notification": {"queued": True},
			"parent_notification": {"queued": True},
			"booking_created": True,
		}
		with patch(
			"qas_custom.modules.makeup.commands._get_active_leave_and_voucher",
			return_value=(None, None),
		), patch(
			"qas_custom.modules.makeup.commands.get_parent_leave_makeup_options_core",
			return_value=options,
		), patch(
			"qas_custom.modules.makeup.commands.submit_parent_leave_request_core",
			return_value=leave_result,
		) as submit_leave, patch(
			"qas_custom.modules.makeup.commands.redeem_parent_voucher_core",
			return_value=booking_result,
		) as redeem, patch(
			"qas_custom.modules.makeup.commands._queue_leave_requested_notification",
			return_value={"queued": True},
		) as queue_leave:
			result = complete_parent_leave_and_redeem_core(
				parent=SimpleNamespace(name="PAR-001"),
				students=[{"name": "STU-LEAVE"}, {"name": "STU-MAKEUP"}],
				student="STU-LEAVE",
				course_session="CS-LEAVE",
				session_id="CS-MAKEUP",
				redeem_student="STU-MAKEUP",
			)

		self.assertEqual(result["voucher"]["status"], "Used")
		submit_leave.assert_called_once_with(
			parent=submit_leave.call_args.kwargs["parent"],
			students=submit_leave.call_args.kwargs["students"],
			student="STU-LEAVE",
			course_session="CS-LEAVE",
			notify_staff=False,
			notify_parent_voucher=False,
		)
		redeem.assert_called_once_with(
			parent=redeem.call_args.kwargs["parent"],
			students=redeem.call_args.kwargs["students"],
			voucher_id="MV-001",
			session_id="CS-MAKEUP",
			student="STU-MAKEUP",
		)
		queue_leave.assert_called_once_with(
			notify_staff=True,
			course_session="CS-LEAVE",
			student="STU-LEAVE",
			leave_request="LR-001",
		)

	def test_book_now_rejects_unpreviewed_target_without_creating_leave(self):
		with patch(
			"qas_custom.modules.makeup.commands._get_active_leave_and_voucher",
			return_value=(None, None),
		), patch(
			"qas_custom.modules.makeup.commands.get_parent_leave_makeup_options_core",
			return_value={"selected_redeem_student": "STU-001", "available_sessions": []},
		), patch(
			"qas_custom.modules.makeup.commands.submit_parent_leave_request_core"
		) as submit_leave, patch(
			"qas_custom.modules.makeup.commands.frappe.throw",
			side_effect=RuntimeError("not available"),
		):
			with self.assertRaisesRegex(Exception, "not available"):
				complete_parent_leave_and_redeem_core(
					parent=SimpleNamespace(name="PAR-001"),
					students=[{"name": "STU-001"}],
					student="STU-001",
					course_session="CS-LEAVE",
					session_id="CS-MAKEUP",
				)

		submit_leave.assert_not_called()

	def test_keep_voucher_is_the_only_path_that_requests_voucher_email(self):
		with patch(
			"qas_custom.modules.makeup.commands._get_active_leave_and_voucher",
			return_value=(None, None),
		), patch(
			"qas_custom.modules.makeup.commands.submit_parent_leave_request_core",
			return_value={"makeup_voucher": "MV-001"},
		) as submit_leave:
			complete_parent_leave_and_keep_voucher_core(
				parent=SimpleNamespace(name="PAR-001"),
				students=[{"name": "STU-001"}],
				student="STU-001",
				course_session="CS-LEAVE",
			)

		submit_leave.assert_called_once_with(
			parent=submit_leave.call_args.kwargs["parent"],
			students=submit_leave.call_args.kwargs["students"],
			student="STU-001",
			course_session="CS-LEAVE",
			notify_staff=True,
			notify_parent_voucher=True,
		)

	@patch("qas_custom.modules.makeup.commands.queue_makeup_voucher_issued_email")
	@patch("qas_custom.modules.makeup.commands.sync_makeup_voucher_label")
	@patch("qas_custom.modules.makeup.commands.today", return_value="2026-07-28")
	def test_book_now_voucher_suppresses_voucher_email(self, _today, _label, queue_email):
		created = FakeDoc(name="MV-001")
		refreshed = FakeDoc(name="MV-001", status="Valid")
		leave = FakeDoc(name="LR-001", student="STU-001", course="Anime", course_session="CS-LEAVE")
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(exists=Mock(return_value=None)),
			new_doc=Mock(return_value=created),
			get_doc=Mock(return_value=refreshed),
		)

		with patch("qas_custom.modules.makeup.commands.frappe", fake_frappe):
			_ensure_leave_makeup_voucher(leave, notify_parent=False)

		self.assertTrue(created.inserted)
		queue_email.assert_not_called()

	def test_retried_book_returns_the_existing_makeup_booking(self):
		leave = FakeDoc(name="LR-001")
		voucher = FakeDoc(name="MV-001", student="STU-LEAVE", status="Used", used_on_session="CS-MAKEUP")
		booking_result = {
			"voucher": {"voucher_id": "MV-001", "status": "Used"},
			"attendance_entry": "ATT-MAKEUP",
			"session": {"session_id": "CS-MAKEUP"},
			"notification": {"queued": False, "duplicate": True},
			"parent_notification": {"queued": False, "duplicate": True},
		}
		with patch(
			"qas_custom.modules.makeup.commands._get_active_leave_and_voucher",
			return_value=(leave, voucher),
		), patch(
			"qas_custom.modules.makeup.commands.redeem_parent_voucher_core",
			return_value=booking_result,
		) as redeem, patch(
			"qas_custom.modules.makeup.commands.submit_parent_leave_request_core"
		) as submit_leave:
			result = complete_parent_leave_and_redeem_core(
				parent=SimpleNamespace(name="PAR-001"),
				students=[{"name": "STU-LEAVE"}],
				student="STU-LEAVE",
				course_session="CS-LEAVE",
				session_id="CS-MAKEUP",
			)

		self.assertTrue(result["duplicate"])
		self.assertFalse(result["booking_created"])
		redeem.assert_called_once()
		submit_leave.assert_not_called()
