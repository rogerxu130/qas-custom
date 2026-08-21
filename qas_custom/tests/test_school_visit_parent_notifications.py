from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.notifications.school_visit_parent_notifications import (
	_school_visit_email_message,
	_school_visit_parent_context,
	classify_school_visit_booking_change,
	queue_school_visit_parent_booking_change,
	school_visit_parent_email_enabled,
	school_visit_parent_event_key,
	school_visit_signature,
	send_school_visit_parent_email_job,
)
from qas_custom.qas_custom.doctype.inquiry.inquiry import Inquiry
from qas_custom.services.inquiry import _resolve_school_visit_context


def inquiry(**overrides):
	values = {
		"name": "INQ-VISIT-001",
		"inquiry_type": "School Visit",
		"status": "Booked",
		"campus": "Indooroopilly",
		"current_appointment_date": "2026-08-22",
		"current_appointment_time": "09:30:00",
		"contact_name": "Pat <Parent>",
		"contact_email": "parent@example.com",
	}
	values.update(overrides)
	return frappe._dict(values)


def email_context(**overrides):
	values = {
		"recipient": {"email": "parent@example.com", "for_user": "parent@example.com"},
		"parent_name": "Pat <Parent>",
		"campus": "Indooroopilly <West>",
		"campus_address": "1 Art <Street>\nBrisbane",
		"date_display": "22 August 2026",
		"time_display": "9:30 AM",
		"duration_minutes": 15,
		"school_name": "Queensland <Art> School",
		"school_email": "school@example.com",
		"school_phone": "0422 688 077",
	}
	values.update(overrides)
	return values


class TestSchoolVisitParentClassification(TestCase):
	def test_new_valid_school_visit_is_booking(self):
		self.assertEqual(classify_school_visit_booking_change(inquiry()), "booked")

	def test_needs_review_and_non_visit_are_ignored(self):
		self.assertIsNone(classify_school_visit_booking_change(inquiry(status="Needs Review")))
		self.assertIsNone(classify_school_visit_booking_change(inquiry(inquiry_type="Trial Lesson")))

	def test_repaired_needs_review_is_first_booking_even_when_status_is_rescheduled(self):
		old = inquiry(
			status="Needs Review",
			campus=None,
			current_appointment_date=None,
			current_appointment_time=None,
		)
		self.assertEqual(classify_school_visit_booking_change(inquiry(status="Rescheduled"), old), "booked")

	def test_changed_active_appointment_is_rescheduled(self):
		old = inquiry()
		self.assertEqual(
			classify_school_visit_booking_change(inquiry(current_appointment_time="13:30:00", status="Rescheduled"), old),
			"rescheduled",
		)
		self.assertEqual(
			classify_school_visit_booking_change(inquiry(campus="Upper Mount Gravatt", status="Rescheduled"), old),
			"rescheduled",
		)

	def test_unchanged_active_appointment_and_inactive_statuses_are_ignored(self):
		self.assertIsNone(classify_school_visit_booking_change(inquiry(), inquiry()))
		for status in ["New", "Cancelled", "Completed", "No-show", "Follow-up", "Converted", "Inactive"]:
			with self.subTest(status=status):
				self.assertIsNone(classify_school_visit_booking_change(inquiry(status=status)))

	def test_missing_required_booking_context_is_ignored(self):
		for fieldname in ["campus", "current_appointment_date", "current_appointment_time", "contact_email"]:
			with self.subTest(fieldname=fieldname):
				self.assertIsNone(classify_school_visit_booking_change(inquiry(**{fieldname: None})))

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.school_visit_parent_email_enabled", return_value=True)
	def test_queue_is_after_commit_and_uses_current_signature(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_exists,
		mock_enqueue,
	):
		doc = inquiry()
		result = queue_school_visit_parent_booking_change(doc)
		self.assertTrue(result["queued"])
		kwargs = mock_enqueue.call_args.kwargs
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["event_kind"], "booked")
		self.assertEqual(kwargs["appointment_signature"], school_visit_signature(doc))

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._notification_event_exists", return_value=True)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.school_visit_parent_email_enabled", return_value=True)
	def test_existing_event_does_not_queue_again(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_exists,
		mock_enqueue,
	):
		result = queue_school_visit_parent_booking_change(inquiry())
		self.assertTrue(result["duplicate"])
		mock_enqueue.assert_not_called()


class TestSchoolVisitParentContent(TestCase):
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._", side_effect=lambda value: value)
	def test_booking_and_reschedule_copy_escape_dynamic_values(self, _mock_translate):
		context = email_context()
		booked = _school_visit_email_message(context, "booked")
		rescheduled = _school_visit_email_message(context, "rescheduled")
		for message in (booked, rescheduled):
			self.assertIn("Pat &lt;Parent&gt;", message)
			self.assertIn("Indooroopilly &lt;West&gt;", message)
			self.assertIn("1 Art &lt;Street&gt;<br>Brisbane", message)
			self.assertIn("15 minutes", message)
		self.assertIn("booked successfully", booked)
		self.assertIn("has been rescheduled", rescheduled)

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.get_invoice_settings")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.db.get_value")
	def test_context_uses_current_visit_details_and_school_contact(self, mock_get_value, mock_settings):
		mock_get_value.side_effect = [
			{"name": "Indooroopilly", "address": "1 Art Street\nBrisbane"},
			{"linked_user": "parent@example.com"},
		]
		mock_settings.return_value = {
			"school_name": "Queensland Art School",
			"school_email": "school@example.com",
			"school_phone": "0422 688 077",
		}
		context = _school_visit_parent_context(inquiry(parent="PAR-001"))
		self.assertEqual(context["recipient"]["email"], "parent@example.com")
		self.assertEqual(context["campus_address"], "1 Art Street\nBrisbane")
		self.assertEqual(context["date_display"], "22 August 2026")
		self.assertEqual(context["time_display"], "9:30 AM")


class TestSchoolVisitParentWorker(TestCase):
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._send_automatic_school_visit_parent_email")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.get_doc")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.school_visit_parent_email_enabled", return_value=True)
	def test_worker_sends_only_current_signature(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_get_doc,
		_mock_exists,
		mock_send,
	):
		doc = inquiry()
		mock_get_doc.return_value = doc
		mock_send.return_value = {"sent": True}
		result = send_school_visit_parent_email_job(doc.name, school_visit_signature(doc), "booked")
		self.assertTrue(result["sent"])
		mock_send.assert_called_once()

		mock_send.reset_mock()
		result = send_school_visit_parent_email_job(doc.name, "stale-signature", "booked")
		self.assertTrue(result["skipped"])
		mock_send.assert_not_called()

	def test_event_keys_are_stable_and_change_with_signature(self):
		signature = school_visit_signature(inquiry())
		booked = school_visit_parent_event_key("booked", "INQ-VISIT-001", signature)
		self.assertEqual(booked, school_visit_parent_event_key("booked", "INQ-VISIT-001", signature))
		self.assertNotEqual(booked, school_visit_parent_event_key("rescheduled", "INQ-VISIT-001", signature))
		self.assertNotEqual(booked, school_visit_parent_event_key("booked", "INQ-VISIT-001", "other"))

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._mark_notification_sent")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.sendmail_or_skip")
	@patch(
		"qas_custom.modules.notifications.school_visit_parent_notifications._reserve_notification_event",
		return_value="LOG-001",
	)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._school_visit_parent_context")
	def test_sender_reserves_then_sends_to_parent_with_school_reply_to(
		self,
		mock_context,
		mock_reserve,
		mock_sendmail,
		mock_queued,
		mock_sent,
	):
		from qas_custom.modules.notifications.school_visit_parent_notifications import (
			_send_automatic_school_visit_parent_email,
		)

		mock_context.return_value = email_context()
		mock_sendmail.return_value = None
		result = _send_automatic_school_visit_parent_email(inquiry(), "booked", "event-key")
		self.assertTrue(result["sent"])
		mock_reserve.assert_called_once()
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["parent@example.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["reply_to"], "school@example.com")
		mock_queued.assert_called_once_with("LOG-001")
		mock_sent.assert_called_once_with("LOG-001")

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._mark_notification_failed")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._mark_notification_queued")
	@patch(
		"qas_custom.modules.notifications.school_visit_parent_notifications.sendmail_or_skip",
		side_effect=RuntimeError("SMTP unavailable"),
	)
	@patch(
		"qas_custom.modules.notifications.school_visit_parent_notifications._reserve_notification_event",
		return_value="LOG-001",
	)
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._school_visit_parent_context")
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.log_error")
	def test_sender_records_failed_delivery_without_raising(
		self,
		_mock_log_error,
		mock_context,
		_mock_reserve,
		_mock_sendmail,
		mock_queued,
		mock_failed,
	):
		from qas_custom.modules.notifications.school_visit_parent_notifications import (
			_send_automatic_school_visit_parent_email,
		)

		mock_context.return_value = email_context()
		result = _send_automatic_school_visit_parent_email(inquiry(), "booked", "event-key")
		self.assertEqual(result, {"sent": False, "reason": "Email send failed."})
		mock_queued.assert_called_once_with("LOG-001")
		mock_failed.assert_called_once_with("LOG-001", "Email send failed.")

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications._reserve_failed_event")
	@patch(
		"qas_custom.modules.notifications.school_visit_parent_notifications._school_visit_parent_context",
		side_effect=Exception("Missing campus address"),
	)
	def test_missing_context_records_failure_without_raising(self, _mock_context, mock_failed):
		from qas_custom.modules.notifications.school_visit_parent_notifications import (
			_send_automatic_school_visit_parent_email,
		)

		result = _send_automatic_school_visit_parent_email(inquiry(), "booked", "event-key")
		self.assertFalse(result["sent"])
		mock_failed.assert_called_once_with("event-key", "INQ-VISIT-001", "Missing campus address")


class TestSchoolVisitParentControllerAndValidation(TestCase):
	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.queue_school_visit_parent_booking_change")
	@patch("qas_custom.modules.notifications.inquiry_admin_notifications.queue_inquiry_admin_notification")
	@patch("qas_custom.modules.notifications.commands.enqueue_session_staff_notification")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.queue_trial_parent_booking_change")
	@patch("qas_custom.services.inquiry.ensure_inquiry_attendance_entry")
	def test_after_insert_queues_both_type_specific_classifiers(
		self,
		_mock_attendance,
		_mock_trial,
		_mock_staff,
		_mock_admin,
		mock_visit,
	):
		doc = inquiry()
		Inquiry.after_insert(doc)
		mock_visit.assert_called_once_with(doc)

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.queue_school_visit_parent_booking_change")
	@patch("qas_custom.modules.notifications.commands.enqueue_session_staff_notification")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.queue_trial_parent_booking_change")
	def test_on_update_passes_previous_document_to_visit_classifier(self, _mock_trial, _mock_staff, mock_visit):
		old = inquiry(status="Needs Review")
		doc = inquiry(status="Rescheduled")
		doc.get_doc_before_save = Mock(return_value=old)
		Inquiry.on_update(doc)
		mock_visit.assert_called_once_with(doc, old_doc=old)

	@patch("qas_custom.services.inquiry._resolve_campus", return_value="Indooroopilly")
	@patch("qas_custom.services.inquiry._parse_school_visit_appointment", return_value=("2026-08-22", "09:30:00", None))
	def test_missing_email_requires_review_even_when_phone_exists(self, _mock_appointment, _mock_campus):
		_context, reason = _resolve_school_visit_context(
			{
				"parent_name": "Pat Parent",
				"contact_phone": "0400000000",
				"campus": "Indooroopilly",
			}
		)
		self.assertIn("Email is required", reason)

	@patch("qas_custom.modules.notifications.school_visit_parent_notifications.frappe.conf", {})
	def test_feature_switch_defaults_to_enabled(self):
		self.assertTrue(school_visit_parent_email_enabled())

	@patch(
		"qas_custom.modules.notifications.school_visit_parent_notifications.frappe.conf",
		{"qas_school_visit_parent_email_enabled": 0},
	)
	def test_feature_switch_can_disable_email(self):
		self.assertFalse(school_visit_parent_email_enabled())
