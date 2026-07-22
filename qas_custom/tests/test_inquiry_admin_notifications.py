from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.notifications.inquiry_admin_notifications import (
	DEFAULT_INQUIRY_NOTIFICATION_EMAIL,
	_inquiry_admin_message,
	_inquiry_admin_subject,
	inquiry_admin_event_key,
	inquiry_notification_email,
	queue_inquiry_admin_notification,
	send_inquiry_admin_notification_job,
)
from qas_custom.qas_custom.doctype.inquiry.inquiry import Inquiry


def inquiry(**overrides):
	values = {
		"name": "INQ-2026-00100",
		"inquiry_type": "Trial Lesson",
		"status": "Booked",
		"source": "Website",
		"creation": "2026-07-22 10:00:00",
		"parent": "parent@example.com",
		"student": "Sam <Student>-2017-01-01",
		"contact_name": "Pat <Parent>",
		"contact_email": "parent@example.com",
		"contact_phone": "+61 400 000 000",
		"campus": "Indooroopilly",
		"preferred_course": "Creative Art - Beginner",
		"current_appointment_date": "2026-07-25",
		"current_appointment_time": "16:00:00",
		"course_session": "CS-001",
	}
	values.update(overrides)
	return frappe._dict(values)


class TestInquiryAdminNotificationQueue(TestCase):
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=False)
	def test_every_named_inquiry_queues_one_after_commit_job(self, _mock_exists, mock_enqueue):
		for inquiry_type, status in [("Trial Lesson", "Booked"), ("School Visit", "New"), ("Trial Lesson", "Needs Review")]:
			with self.subTest(inquiry_type=inquiry_type, status=status):
				mock_enqueue.reset_mock()
				result = queue_inquiry_admin_notification(
					inquiry(inquiry_type=inquiry_type, status=status, student=None, course_session=None)
				)
				self.assertTrue(result["queued"])
				kwargs = mock_enqueue.call_args.kwargs
				self.assertTrue(kwargs["enqueue_after_commit"])
				self.assertTrue(kwargs["deduplicate"])
				self.assertEqual(kwargs["inquiry"], "INQ-2026-00100")
				self.assertEqual(result["recipient"], DEFAULT_INQUIRY_NOTIFICATION_EMAIL)

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=True)
	def test_existing_event_prevents_duplicate_queue(self, _mock_exists, mock_enqueue):
		result = queue_inquiry_admin_notification(inquiry())
		self.assertTrue(result["duplicate"])
		mock_enqueue.assert_not_called()

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.log_error")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.enqueue", side_effect=Exception("Redis unavailable"))
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=False)
	def test_queue_failure_does_not_raise_into_inquiry_insert(self, _mock_exists, _mock_enqueue, mock_log_error):
		result = queue_inquiry_admin_notification(inquiry())
		self.assertFalse(result["queued"])
		self.assertIn("could not be queued", result["reason"])
		mock_log_error.assert_called_once()

	def test_event_key_is_stable_and_specific_to_inquiry(self):
		self.assertEqual(inquiry_admin_event_key("INQ-001"), inquiry_admin_event_key("INQ-001"))
		self.assertNotEqual(inquiry_admin_event_key("INQ-001"), inquiry_admin_event_key("INQ-002"))

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.conf", {})
	def test_recipient_defaults_to_confirmed_admin_email(self):
		self.assertEqual(inquiry_notification_email(), "rogerxu130@gmail.com")

	@patch(
		"qas_custom.modules.notifications.inquiry_admin_notifications.frappe.conf",
		{"qas_inquiry_notification_email": " Future.Admin@Example.com "},
	)
	def test_recipient_can_be_changed_by_site_configuration(self):
		self.assertEqual(inquiry_notification_email(), "future.admin@example.com")


class TestInquiryAdminNotificationContent(TestCase):
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.conf", {})
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._", side_effect=lambda value: value)
	def test_full_contact_and_booking_details_are_escaped(self, _mock_translate):
		doc = inquiry()
		subject = _inquiry_admin_subject(doc)
		message = _inquiry_admin_message(doc)
		self.assertIn("INQ-2026-00100", subject)
		self.assertIn("Trial Lesson", subject)
		self.assertIn("Pat &lt;Parent&gt;", message)
		self.assertIn("Sam &lt;Student&gt;-2017-01-01", message)
		self.assertIn("parent@example.com", message)
		self.assertIn("Creative Art - Beginner", message)
		self.assertIn("https://portal.queenslandartschool.com/school-admin", message)

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.conf", {})
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._", side_effect=lambda value: value)
	def test_missing_optional_values_use_placeholders(self, _mock_translate):
		message = _inquiry_admin_message(
			inquiry(parent=None, student=None, submitted_student_name=None, course_session=None, submitted_class_session=None)
		)
		self.assertIn(">-</td>", message)


class TestInquiryAdminNotificationWorker(TestCase):
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_sent")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.sendmail_or_skip")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.get_invoice_settings")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._create_notification_log", return_value="LOG-001")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.get_doc")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.db.exists", return_value=True)
	def test_worker_reserves_sends_and_marks_success(
		self,
		_mock_exists,
		mock_get_doc,
		_mock_event,
		mock_create_log,
		mock_settings,
		mock_sendmail,
		mock_queued,
		mock_sent,
	):
		mock_get_doc.return_value = inquiry()
		mock_settings.return_value = {"school_email": "queenslandartschool@gmail.com"}
		mock_sendmail.return_value = None

		result = send_inquiry_admin_notification_job("INQ-2026-00100")

		self.assertTrue(result["sent"])
		self.assertEqual(result["recipient"], "rogerxu130@gmail.com")
		self.assertEqual(mock_create_log.call_args.kwargs["recipient"], {"email": "rogerxu130@gmail.com"})
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["rogerxu130@gmail.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["reference_name"], "INQ-2026-00100")
		self.assertEqual(mock_sendmail.call_args.kwargs["reply_to"], "queenslandartschool@gmail.com")
		mock_queued.assert_called_once_with("LOG-001")
		mock_sent.assert_called_once_with("LOG-001")

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_failed")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.outbound_email_enabled", return_value=False)
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._create_notification_log", return_value="LOG-001")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.get_doc", return_value=inquiry())
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.db.exists", return_value=True)
	def test_disabled_outbound_email_is_audited_without_raising(
		self,
		_mock_exists,
		_mock_doc,
		_mock_event,
		_mock_log,
		_mock_outbound,
		mock_queued,
		mock_failed,
	):
		result = send_inquiry_admin_notification_job("INQ-2026-00100")
		self.assertTrue(result["skipped"])
		mock_queued.assert_called_once_with("LOG-001")
		mock_failed.assert_called_once()

	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_failed")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.sendmail_or_skip", side_effect=Exception("SMTP failed"))
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.get_invoice_settings", return_value={})
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._create_notification_log", return_value="LOG-001")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.get_doc", return_value=inquiry())
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.db.exists", return_value=True)
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.frappe.log_error")
	def test_send_failure_is_audited_without_raising(
		self,
		mock_log_error,
		_mock_exists,
		_mock_doc,
		_mock_event,
		_mock_log,
		_mock_settings,
		_mock_sendmail,
		_mock_queued,
		mock_failed,
	):
		result = send_inquiry_admin_notification_job("INQ-2026-00100")
		self.assertFalse(result["sent"])
		mock_failed.assert_called_once_with("LOG-001", "Email send failed.")
		mock_log_error.assert_called_once()


class TestInquiryAdminNotificationController(TestCase):
	@patch("qas_custom.modules.notifications.commands.enqueue_session_staff_notification")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.queue_inquiry_admin_notification")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.queue_trial_parent_booking_change")
	@patch("qas_custom.services.inquiry.ensure_inquiry_attendance_entry")
	def test_school_visit_without_student_or_session_still_queues_admin_email(
		self,
		_mock_attendance,
		_mock_parent,
		mock_admin,
		mock_staff,
	):
		doc = SimpleNamespace(
			name="INQ-2026-00101",
			inquiry_type="School Visit",
			status="New",
			student=None,
			course_session=None,
		)
		Inquiry.after_insert(doc)
		mock_admin.assert_called_once_with(doc)
		mock_staff.assert_not_called()
