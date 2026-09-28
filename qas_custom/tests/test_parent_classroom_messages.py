from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.parent_classroom_messages import (
	_email_body,
	_valid_category,
	_valid_message,
	create_teacher_parent_classroom_message_data,
	retry_teacher_parent_classroom_message_data,
	send_parent_classroom_message_job,
)


class FakeMessage:
	def __init__(self, **values):
		self.attempts = []
		self.name = values.pop("name", "PCM-TEST")
		for key, value in values.items():
			setattr(self, key, value)
		self.insert = Mock()
		self.save = Mock()

	def append(self, _fieldname, values):
		row = frappe._dict(values)
		self.attempts.append(row)
		return row


class TestParentClassroomMessageCreation(TestCase):
	def test_create_and_retry_are_disabled_without_database_or_queue_writes(self):
		fake_frappe = SimpleNamespace(
			PermissionError=PermissionError,
			throw=Mock(side_effect=PermissionError("Messaging disabled")),
			db=Mock(), new_doc=Mock(), get_doc=Mock(),
		)
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.reject_support_view_write"
		), patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe), patch(
			"qas_custom.services.parent_classroom_messages._queue_delivery_or_mark_failed"
		) as queue_delivery:
			for action, args in (
				(create_teacher_parent_classroom_message_data, ("SESSION-1", "ATT-1", "STU-1", "Participation", "Hello", "request-1")),
				(retry_teacher_parent_classroom_message_data, ("PCM-TEST",)),
			):
				with self.subTest(action=action.__name__), self.assertRaises(PermissionError):
					action(*args)
			queue_delivery.assert_not_called()
		self.assertEqual(fake_frappe.db.mock_calls, [])
		fake_frappe.new_doc.assert_not_called()
		fake_frappe.get_doc.assert_not_called()

	def test_content_validation_keeps_v1_categories_and_limit(self):
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.frappe.throw",
			side_effect=lambda message, *args: (_ for _ in ()).throw(frappe.ValidationError(message)),
		):
			self.assertEqual(_valid_category("Behaviour concern"), "Behaviour concern")
			self.assertEqual(_valid_message("  Plain text  "), "Plain text")
			with self.assertRaises(frappe.ValidationError):
				_valid_category("Urgent escalation")
			with self.assertRaises(frappe.ValidationError):
				_valid_message("x" * 2001)


class TestParentClassroomMessageDelivery(TestCase):
	def test_queued_job_is_marked_failed_without_sending(self):
		attempt = frappe._dict(attempt_number=1, status="Queued")
		doc = FakeMessage(status="Queued", message="Original immutable message")
		doc.attempts = [attempt]
		fake_frappe = SimpleNamespace(db=SimpleNamespace(sql=Mock(), commit=Mock()), get_doc=Mock(return_value=doc))
		with patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe), patch(
			"qas_custom.services.parent_classroom_messages.now_datetime", return_value="2026-09-28 10:00:00"
		), patch("qas_custom.utils.environment.sendmail_or_skip") as sendmail:
			result = send_parent_classroom_message_job("PCM-TEST", 1)
			repeated = send_parent_classroom_message_job("PCM-TEST", 1)
		self.assertFalse(result["sent"])
		self.assertTrue(result["skipped"])
		self.assertTrue(repeated["skipped"])
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(attempt.status, "Failed")
		self.assertIn("disabled", attempt.error_summary)
		self.assertEqual(doc.message, "Original immutable message")
		doc.save.assert_called_once_with(ignore_permissions=True)
		fake_frappe.db.commit.assert_called_once_with()
		sendmail.assert_not_called()

	def test_completed_jobs_are_left_unchanged(self):
		for status in ("Sent", "Failed"):
			with self.subTest(status=status):
				attempt = frappe._dict(attempt_number=1, status=status)
				doc = FakeMessage(status=status)
				doc.attempts = [attempt]
				fake_frappe = SimpleNamespace(db=SimpleNamespace(sql=Mock()), get_doc=Mock(return_value=doc))
				with patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe):
					result = send_parent_classroom_message_job("PCM-TEST", 1)
				self.assertTrue(result["skipped"])
				self.assertEqual(doc.status, status)
				doc.save.assert_not_called()

	def test_email_body_escapes_teacher_content(self):
		doc = frappe._dict(
			student="STU-1", parent="PAR-1", teacher="Teacher One", course_session="SESSION-1",
			category="Behaviour concern", message="Please discuss <script>alert(1)</script>\nThank you.",
		)
		values = {
			("Student", "STU-1"): "Alex Student",
			("Parent", "PAR-1"): "Parent One",
			("Teacher", "Teacher One"): "Teacher One",
		}

		def get_value(doctype, name, fields, as_dict=False):
			if doctype == "Course Sessions":
				return frappe._dict(weekly_timeslot="TS-1", session_date="2026-08-20")
			if doctype == "Weekly Timeslot":
				return frappe._dict(course="Drawing")
			return values.get((doctype, name))

		fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=get_value))
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.frappe", fake_frappe
		):
			body = _email_body(doc)

		self.assertNotIn("<script>", body)
		self.assertIn("&lt;script&gt;", body)
		self.assertIn("<br>", body)
