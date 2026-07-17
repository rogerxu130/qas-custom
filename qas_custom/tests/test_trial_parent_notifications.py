from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.notifications.commands import _trial_class_reminder_email_message
from qas_custom.modules.notifications.trial_parent_notifications import (
	_email_copy,
	_existing_notification_events,
	_is_24h_window,
	_reserve_notification_event,
	_was_booked_or_rescheduled_inside_24h,
	booking_email_enabled,
	classify_trial_booking_change,
	queue_trial_parent_booking_change,
	reminder_email_enabled,
	run_trial_parent_24h_reminders,
	send_trial_parent_24h_reminder_job,
	send_trial_parent_booking_email_job,
	trial_parent_event_key,
)
from qas_custom.qas_custom.doctype.inquiry.inquiry import Inquiry


def inquiry(**overrides):
	values = {
		"name": "INQ-001",
		"inquiry_type": "Trial Lesson",
		"status": "Booked",
		"course_session": "CS-001",
		"student": "STU-001",
	}
	values.update(overrides)
	return frappe._dict(values)


def email_context(**overrides):
	values = {
		"recipient": {"email": "parent@example.com", "for_user": "parent@example.com"},
		"parent_name": "Pat <Parent>",
		"student_name": "Sam <Student>",
		"course": "Creative <Art>",
		"campus": "Indooroopilly",
		"campus_address": "1 Art Street\nBrisbane",
		"date_display": "18 July 2026",
		"start_time": "16:00",
		"end_time": "17:00",
		"school_name": "Queensland Art School",
		"school_email": "queenslandartschool@gmail.com",
		"school_phone": "0422 688 077",
	}
	values.update(overrides)
	return values


class TestTrialParentBookingClassification(TestCase):
	def test_new_booked_trial_is_booking(self):
		self.assertEqual(classify_trial_booking_change(inquiry()), "booked")

	def test_needs_review_and_non_trial_are_ignored(self):
		self.assertIsNone(classify_trial_booking_change(inquiry(status="Needs Review")))
		self.assertIsNone(classify_trial_booking_change(inquiry(inquiry_type="School Visit")))

	def test_transition_from_needs_review_to_booked_is_booking(self):
		old = frappe._dict(status="Needs Review", course_session=None)
		self.assertEqual(classify_trial_booking_change(inquiry(), old), "booked")

	def test_same_booked_session_is_not_duplicated(self):
		old = frappe._dict(status="Booked", course_session="CS-001")
		self.assertIsNone(classify_trial_booking_change(inquiry(), old))

	def test_course_session_change_and_new_rescheduled_status_are_reschedules(self):
		old = frappe._dict(status="Booked", course_session="CS-001")
		self.assertEqual(
			classify_trial_booking_change(inquiry(status="Booked", course_session="CS-002"), old),
			"rescheduled",
		)
		self.assertEqual(
			classify_trial_booking_change(inquiry(status="Rescheduled"), old),
			"rescheduled",
		)

	def test_inactive_lifecycle_states_are_ignored(self):
		for status in ["Cancelled", "Completed", "No-show", "Follow-up", "Converted", "Inactive"]:
			with self.subTest(status=status):
				self.assertIsNone(classify_trial_booking_change(inquiry(status=status)))

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.booking_email_enabled", return_value=True)
	def test_queue_uses_after_commit_deterministic_deduplicated_job(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_exists,
		mock_enqueue,
	):
		result = queue_trial_parent_booking_change(inquiry())
		self.assertTrue(result["queued"])
		kwargs = mock_enqueue.call_args.kwargs
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["event_kind"], "booked")
		self.assertEqual(kwargs["course_session"], "CS-001")
		self.assertIn("trial_parent_booked-", kwargs["job_id"])

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.booking_email_enabled", return_value=False)
	def test_booking_switch_blocks_queue(self, _mock_enabled, mock_enqueue):
		result = queue_trial_parent_booking_change(inquiry())
		self.assertTrue(result["skipped"])
		mock_enqueue.assert_not_called()


class TestTrialParentEmailContent(TestCase):
	@patch("qas_custom.modules.notifications.trial_parent_notifications._", side_effect=lambda value: value)
	def test_booking_reschedule_and_reminder_copy(self, _mock_translate):
		context = email_context()
		booked = _email_copy("booked", context)
		rescheduled = _email_copy("rescheduled", context)
		reminder = _email_copy("reminder_24h", context)
		self.assertEqual(booked[0], "Trial Class Booked: Sam <Student> — 18 July 2026")
		self.assertIn("booked successfully", booked[2])
		self.assertEqual(rescheduled[0], "Trial Class Rescheduled: Sam <Student> — 18 July 2026")
		self.assertIn("updated class details", rescheduled[2])
		self.assertEqual(reminder[0], "Reminder: Sam <Student>'s Trial Class on 18 July 2026")
		self.assertIn("friendly reminder", reminder[2])

	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	def test_template_preserves_manual_defaults_and_escapes_custom_copy(self, _mock_translate):
		context = email_context()
		manual_message = _trial_class_reminder_email_message(context)
		self.assertIn("Trial class reminder", manual_message)
		self.assertIn("friendly reminder", manual_message)
		self.assertIn("Sam &lt;Student&gt;", manual_message)
		self.assertIn("Creative &lt;Art&gt;", manual_message)
		self.assertIn("1 Art Street<br>Brisbane", manual_message)
		self.assertIn("All art supplies are provided for the trial class.", manual_message)

		custom_message = _trial_class_reminder_email_message(
			context,
			heading="Booked <now>",
			intro="Safe <copy>",
		)
		self.assertIn("Booked &lt;now&gt;", custom_message)
		self.assertIn("Safe &lt;copy&gt;", custom_message)
		self.assertIn("All art supplies are provided for the trial class.", custom_message)


class TestTrialParentReminderTiming(TestCase):
	def test_24h_window_uses_brisbane_wall_clock(self):
		brisbane = timezone(timedelta(hours=10))
		class_start = datetime(2026, 7, 18, 16, 0)
		self.assertTrue(_is_24h_window(datetime(2026, 7, 17, 16, 0, tzinfo=brisbane), class_start))
		self.assertTrue(_is_24h_window(datetime(2026, 7, 17, 16, 15, tzinfo=brisbane), class_start))
		self.assertFalse(_is_24h_window(datetime(2026, 7, 17, 15, 59, 59, tzinfo=brisbane), class_start))
		self.assertFalse(_is_24h_window(datetime(2026, 7, 17, 16, 15, 1, tzinfo=brisbane), class_start))

	def test_event_keys_are_stable_short_and_distinct(self):
		booked = trial_parent_event_key("booked", "INQ-001", "CS-001")
		self.assertEqual(booked, trial_parent_event_key("booked", "INQ-001", "CS-001"))
		self.assertNotEqual(booked, trial_parent_event_key("rescheduled", "INQ-001", "CS-001"))
		self.assertNotEqual(booked, trial_parent_event_key("booked", "INQ-001", "CS-002"))
		self.assertLessEqual(len(booked), 140)

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.enqueue")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._existing_notification_events")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._get_24h_candidates")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.get_datetime_in_timezone")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.reminder_email_enabled", return_value=True)
	def test_scheduler_batches_existing_logs_and_queues_due_inquiries(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_timezone,
		mock_candidates,
		mock_existing,
		mock_enqueue,
	):
		now = datetime(2026, 7, 17, 16, 0)
		mock_timezone.return_value = now
		mock_candidates.return_value = [
			{"name": "INQ-001", "course_session": "CS-001"},
			{"name": "INQ-002", "course_session": "CS-002"},
		]
		mock_existing.return_value = {trial_parent_event_key("reminder_24h", "INQ-002", "CS-002")}

		result = run_trial_parent_24h_reminders()

		mock_timezone.assert_called_once_with("Australia/Brisbane")
		mock_existing.assert_called_once()
		mock_enqueue.assert_called_once()
		self.assertEqual(mock_enqueue.call_args.kwargs["inquiry"], "INQ-001")
		self.assertEqual(result, {"eligible": 2, "queued": 1, "skipped": 1, "failed": 0})

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.get_all")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.get_meta")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_log_available", return_value=True)
	def test_existing_events_use_one_batched_standard_field_query(self, _mock_available, mock_meta, mock_get_all):
		mock_meta.return_value.has_field.return_value = False
		mock_get_all.return_value = ["key-1"]
		self.assertEqual(_existing_notification_events(["key-1", "key-2"]), {"key-1"})
		self.assertEqual(mock_get_all.call_count, 1)
		filter_value = mock_get_all.call_args.kwargs["filters"]["document_name"]
		self.assertEqual(filter_value[0], "in")
		self.assertEqual(set(filter_value[1]), {"key-1", "key-2"})

	@patch("qas_custom.modules.notifications.trial_parent_notifications._system_datetime_to_brisbane")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.get_all")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.get_meta")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_log_available", return_value=True)
	def test_recent_booking_suppression_works_with_standard_notification_log(
		self,
		_mock_available,
		mock_meta,
		mock_get_all,
		mock_convert,
	):
		mock_meta.return_value.has_field.return_value = False
		mock_get_all.return_value = [{"creation": "2026-07-17 16:05:00"}]
		mock_convert.return_value = datetime(2026, 7, 17, 16, 5)
		class_start = datetime(2026, 7, 18, 16, 0)
		self.assertTrue(_was_booked_or_rescheduled_inside_24h("INQ-001", "CS-001", class_start))
		self.assertIn("document_name", mock_get_all.call_args.kwargs["filters"])


class TestTrialParentWorkers(TestCase):
	@patch("qas_custom.modules.notifications.trial_parent_notifications._get_current_trial_inquiry", return_value=None)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.booking_email_enabled", return_value=True)
	def test_booking_worker_revalidates_stale_session(self, _mock_enabled, _mock_outbound, _mock_current):
		result = send_trial_parent_booking_email_job("INQ-001", "CS-OLD", "booked")
		self.assertTrue(result["skipped"])
		self.assertIn("no longer booked", result["reason"])

	@patch("qas_custom.modules.notifications.trial_parent_notifications._send_automatic_trial_parent_email")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.trial_parent_notifications._was_booked_or_rescheduled_inside_24h", return_value=True)
	@patch("qas_custom.modules.notifications.trial_parent_notifications._get_class_start")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._get_current_trial_inquiry")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.trial_parent_notifications.reminder_email_enabled", return_value=True)
	def test_24h_worker_skips_when_booking_was_inside_24h(
		self,
		_mock_enabled,
		_mock_outbound,
		mock_current,
		mock_start,
		_mock_recent,
		_mock_exists,
		mock_send,
	):
		now = datetime(2026, 7, 17, 16, 0)
		mock_current.return_value = inquiry()
		mock_start.return_value = datetime(2026, 7, 18, 16, 0)
		result = send_trial_parent_24h_reminder_job("INQ-001", "CS-001", now=now)
		self.assertTrue(result["skipped"])
		mock_send.assert_not_called()

	@patch("qas_custom.modules.notifications.trial_parent_notifications._mark_notification_sent")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._mark_notification_queued")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.sendmail_or_skip")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._reserve_notification_event", return_value="LOG-001")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._trial_class_reminder_context")
	def test_sender_reserves_then_sends_to_parent_with_school_reply_to(
		self,
		mock_context,
		mock_reserve,
		mock_sendmail,
		mock_queued,
		mock_sent,
	):
		from qas_custom.modules.notifications.trial_parent_notifications import _send_automatic_trial_parent_email

		mock_context.return_value = email_context()
		mock_sendmail.return_value = None
		result = _send_automatic_trial_parent_email(inquiry(), "booked", "event-key")
		self.assertTrue(result["sent"])
		mock_reserve.assert_called_once()
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["parent@example.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["reply_to"], "queenslandartschool@gmail.com")
		mock_queued.assert_called_once_with("LOG-001")
		mock_sent.assert_called_once_with("LOG-001")

	@patch("qas_custom.modules.notifications.trial_parent_notifications._reserve_failed_event")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._trial_class_reminder_context", side_effect=Exception("Missing parent email"))
	def test_missing_context_records_failure_without_raising(self, _mock_context, mock_failed):
		from qas_custom.modules.notifications.trial_parent_notifications import _send_automatic_trial_parent_email

		result = _send_automatic_trial_parent_email(inquiry(), "booked", "event-key")
		self.assertFalse(result["sent"])
		mock_failed.assert_called_once_with("event-key", "INQ-001", "Missing parent email")

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.new_doc")
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.trial_parent_notifications._notification_log_available", return_value=True)
	def test_reservation_uses_standard_document_name_fallback(
		self,
		_mock_available,
		_mock_exists,
		mock_new_doc,
	):
		log = SimpleNamespace(
			meta=SimpleNamespace(has_field=lambda fieldname: fieldname == "for_user"),
			flags=SimpleNamespace(),
			insert=Mock(),
			name="LOG-001",
		)
		mock_new_doc.return_value = log
		cache = SimpleNamespace(lock=Mock(return_value=nullcontext()))
		with patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.cache", cache), patch(
			"qas_custom.modules.notifications.trial_parent_notifications.frappe.session",
			SimpleNamespace(user="Administrator"),
		):
			name = _reserve_notification_event(
				"trial_parent_booked:key",
				{"email": "parent@example.com"},
				"Subject",
				"Message",
				"INQ-001",
			)
		self.assertEqual(name, "LOG-001")
		self.assertEqual(log.document_name, "trial_parent_booked:key")
		log.insert.assert_called_once_with(ignore_permissions=True)


class TestTrialParentControllerAndSwitches(TestCase):
	@patch("qas_custom.modules.notifications.commands.enqueue_session_staff_notification")
	@patch("qas_custom.modules.notifications.trial_parent_notifications.queue_trial_parent_booking_change")
	@patch("qas_custom.services.inquiry.ensure_inquiry_attendance_entry")
	def test_after_insert_queues_parent_email_after_attendance(
		self,
		mock_attendance,
		mock_parent,
		_mock_staff,
	):
		doc = inquiry()
		Inquiry.after_insert(doc)
		mock_attendance.assert_called_once_with(doc)
		mock_parent.assert_called_once_with(doc)

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.conf", {})
	def test_switches_default_to_enabled(self):
		self.assertTrue(booking_email_enabled())
		self.assertTrue(reminder_email_enabled())

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.conf", {"qas_trial_parent_booking_email_enabled": 0})
	def test_booking_switch_does_not_disable_reminder(self):
		self.assertFalse(booking_email_enabled())
		self.assertTrue(reminder_email_enabled())

	@patch("qas_custom.modules.notifications.trial_parent_notifications.frappe.conf", {"qas_trial_parent_24h_reminder_enabled": 0})
	def test_reminder_switch_does_not_disable_booking(self):
		self.assertTrue(booking_email_enabled())
		self.assertFalse(reminder_email_enabled())
