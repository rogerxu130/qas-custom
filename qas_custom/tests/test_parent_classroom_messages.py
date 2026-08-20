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
	def test_create_snapshots_context_and_queues_once(self):
		doc = FakeMessage()
		teacher = frappe._dict(name="Teacher One")
		context = {
			"session": frappe._dict(name="SESSION-1"),
			"attendance": frappe._dict(name="ATT-1"),
			"student": frappe._dict(name="STU-1"),
			"parent": frappe._dict(name="PAR-1"),
			"recipient_email": "parent@example.com",
		}
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(get_value=Mock(return_value=None), commit=Mock()),
			session=SimpleNamespace(user="teacher@example.com"),
			new_doc=Mock(return_value=doc),
		)
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.now_datetime", return_value="2026-08-20 10:00:00"
		), patch("qas_custom.services.parent_classroom_messages.reject_support_view_write"), patch(
			"qas_custom.services.parent_classroom_messages._require_teacher", return_value=teacher
		), patch(
			"qas_custom.services.parent_classroom_messages._teacher_message_context", return_value=context
		), patch(
			"qas_custom.services.parent_classroom_messages._school_mail_context",
			return_value={"sender_email": "school@example.com", "reply_to_email": "school@example.com"},
		), patch(
			"qas_custom.services.parent_classroom_messages._queue_delivery_or_mark_failed"
		) as queue_delivery, patch(
			"qas_custom.services.parent_classroom_messages._message_payload", return_value={"name": "PCM-TEST"}
		), patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe):
			result = create_teacher_parent_classroom_message_data(
				"SESSION-1", "ATT-1", "STU-1", "Participation", "Great focus today.", "request-1"
			)

		self.assertFalse(result["duplicate"])
		self.assertEqual(doc.parent, "PAR-1")
		self.assertEqual(doc.recipient_email, "parent@example.com")
		self.assertEqual(doc.status, "Queued")
		self.assertEqual(len(doc.attempts), 1)
		doc.insert.assert_called_once_with(ignore_permissions=True)
		fake_frappe.db.commit.assert_called_once_with()
		queue_delivery.assert_called_once_with("PCM-TEST", 1)

	def test_duplicate_request_returns_existing_message_without_queueing(self):
		teacher = frappe._dict(name="Teacher One")
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(get_value=Mock(return_value=frappe._dict(name="PCM-OLD", teacher="Teacher One"))),
		)
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.reject_support_view_write"
		), patch(
			"qas_custom.services.parent_classroom_messages._require_teacher", return_value=teacher
		), patch(
			"qas_custom.services.parent_classroom_messages._message_payload", return_value={"name": "PCM-OLD"}
		), patch(
			"qas_custom.services.parent_classroom_messages._queue_delivery_or_mark_failed"
		) as queue_delivery, patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe):
			result = create_teacher_parent_classroom_message_data(client_request_id="request-1")

		self.assertTrue(result["duplicate"])
		self.assertEqual(result["message"]["name"], "PCM-OLD")
		queue_delivery.assert_not_called()

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
	def test_failed_message_retry_appends_attempt_without_changing_content(self):
		doc = FakeMessage(
			name="PCM-TEST", teacher="Teacher One", course_session="SESSION-1", status="Failed", attempt_count=1,
			message="Original immutable message", category="Participation",
		)
		doc.attempts = [frappe._dict(attempt_number=1, status="Failed")]
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(sql=Mock(), commit=Mock()),
			get_doc=Mock(return_value=doc),
			session=SimpleNamespace(user="teacher@example.com"),
		)
		with patch("qas_custom.services.parent_classroom_messages._", side_effect=lambda value: value), patch(
			"qas_custom.services.parent_classroom_messages.now_datetime", return_value="2026-08-20 10:00:00"
		), patch("qas_custom.services.parent_classroom_messages.reject_support_view_write"), patch(
			"qas_custom.services.parent_classroom_messages._require_teacher", return_value=frappe._dict(name="Teacher One")
		), patch("qas_custom.services.parent_classroom_messages._get_owned_session"), patch(
			"qas_custom.services.parent_classroom_messages._school_mail_context",
			return_value={"sender_email": "school@example.com", "reply_to_email": "school@example.com"},
		), patch(
			"qas_custom.services.parent_classroom_messages._queue_delivery_or_mark_failed"
		) as queue_delivery, patch(
			"qas_custom.services.parent_classroom_messages._message_payload", return_value={"name": "PCM-TEST"}
		), patch("qas_custom.services.parent_classroom_messages.frappe", fake_frappe):
			result = retry_teacher_parent_classroom_message_data("PCM-TEST")

		self.assertEqual(result["message"]["name"], "PCM-TEST")
		self.assertEqual(doc.message, "Original immutable message")
		self.assertEqual(doc.attempt_count, 2)
		self.assertEqual(doc.attempts[-1].status, "Queued")
		doc.save.assert_called_once_with(ignore_permissions=True)
		queue_delivery.assert_called_once_with("PCM-TEST", 2)

	def test_send_uses_school_sender_and_reply_to(self):
		attempt = frappe._dict(
			attempt_number=1,
			status="Queued",
			sender_email="school@example.com",
			reply_to_email="school@example.com",
		)
		doc = FakeMessage(
			name="PCM-TEST", status="Queued", recipient_email="parent@example.com", student="STU-1",
			parent="PAR-1", teacher="Teacher One", course_session="SESSION-1", category="Participation",
			message="Great focus today.",
		)
		doc.attempts = [attempt]
		fake_frappe = SimpleNamespace(db=SimpleNamespace(sql=Mock()), get_doc=Mock(return_value=doc))
		with patch("qas_custom.services.parent_classroom_messages.sendmail_or_skip", return_value=None) as sendmail, patch(
			"qas_custom.services.parent_classroom_messages._email_subject", return_value="Subject"
		), patch("qas_custom.services.parent_classroom_messages._email_body", return_value="Body"), patch(
			"qas_custom.services.parent_classroom_messages._sender_label", return_value="Queensland Art School <school@example.com>"
		), patch("qas_custom.services.parent_classroom_messages._mark_attempt") as mark_attempt, patch(
			"qas_custom.services.parent_classroom_messages.frappe", fake_frappe
		):
			result = send_parent_classroom_message_job("PCM-TEST", 1)

		self.assertTrue(result["sent"])
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["parent@example.com"])
		self.assertEqual(sendmail.call_args.kwargs["sender"], "Queensland Art School <school@example.com>")
		self.assertEqual(sendmail.call_args.kwargs["reply_to"], "school@example.com")
		mark_attempt.assert_called_once_with(doc, attempt, "Sent", email_queue=None)

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
