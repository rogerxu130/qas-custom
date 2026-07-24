from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.notifications.invoice_overdue_reminders import (
	MAX_REMINDER_ATTEMPTS,
	overdue_reminder_email_message,
	overdue_reminder_event_key,
	overdue_reminder_subject,
	next_reminder_sequence,
	run_overdue_invoice_reminders,
	send_overdue_invoice_reminder_job,
)


def invoice(**overrides):
	values = {
		"name": "ACC-SINV-2026-00001",
		"docstatus": 1,
		"is_return": 0,
		"posting_date": "2026-07-01",
		"due_date": "2026-07-08",
		"outstanding_amount": 120,
		"customer": "CUS-001",
		"customer_name": "Parent One",
		"items": [],
	}
	values.update(overrides)
	return frappe._dict(values)


class TestOverdueReminderCadence(TestCase):
	def test_first_reminder_is_due_one_day_after_due_date(self):
		self.assertIsNone(next_reminder_sequence(invoice(due_date="2026-07-08"), [], date(2026, 7, 8)))
		self.assertEqual(next_reminder_sequence(invoice(due_date="2026-07-08"), [], date(2026, 7, 9)), 1)

	@patch(
		"qas_custom.modules.notifications.invoice_overdue_reminders._system_datetime_to_brisbane",
		side_effect=lambda value: value,
	)
	def test_later_reminders_require_three_full_calendar_days(self, _mock_timezone):
		attempts = [{"creation": datetime(2026, 7, 9, 9, 0)}]
		self.assertIsNone(next_reminder_sequence(invoice(), attempts, date(2026, 7, 11)))
		self.assertEqual(next_reminder_sequence(invoice(), attempts, date(2026, 7, 12)), 2)

	@patch(
		"qas_custom.modules.notifications.invoice_overdue_reminders._system_datetime_to_brisbane",
		side_effect=lambda value: value,
	)
	def test_reminders_stop_after_five_attempts(self, _mock_timezone):
		attempts = [{"creation": datetime(2026, 7, 9, 9, 0) + timedelta(days=index * 3)} for index in range(MAX_REMINDER_ATTEMPTS)]
		self.assertIsNone(next_reminder_sequence(invoice(), attempts, date(2026, 8, 1)))

	def test_event_key_is_deterministic_and_unique_per_sequence(self):
		first = overdue_reminder_event_key("ACC-SINV-2026-00001", "2026-07-08", 1)
		self.assertEqual(first, overdue_reminder_event_key("ACC-SINV-2026-00001", "2026-07-08", 1))
		self.assertNotEqual(first, overdue_reminder_event_key("ACC-SINV-2026-00001", "2026-07-08", 2))
		self.assertLessEqual(len(first), 140)


class TestOverdueReminderScheduler(TestCase):
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._queue_overdue_invoice_reminder")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._get_reminder_history")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._get_overdue_invoice_candidates")
	@patch(
		"qas_custom.modules.notifications.invoice_overdue_reminders._system_datetime_to_brisbane",
		side_effect=lambda value: value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value,
	)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_log_available", return_value=True)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_invoice_reminders_enabled", return_value=True)
	def test_scheduler_queues_due_invoice_and_skips_recent_attempt(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_log,
		_mock_timezone,
		mock_candidates,
		mock_history,
		mock_queue,
	):
		mock_candidates.return_value = [
			{
				"name": "INV-001",
				"due_date": "2026-07-08",
				"outstanding_amount": 120,
				"docstatus": 1,
				"is_return": 0,
			},
			{
				"name": "INV-002",
				"due_date": "2026-07-08",
				"outstanding_amount": 120,
				"docstatus": 1,
				"is_return": 0,
			},
		]
		mock_history.return_value = {
			"INV-001": [],
			"INV-002": [{"creation": datetime(2026, 7, 8, 9, 0, tzinfo=timezone(timedelta(hours=10)))}],
		}
		mock_queue.return_value = {"queued": True}

		result = run_overdue_invoice_reminders(today=date(2026, 7, 9))

		mock_queue.assert_called_once_with("INV-001", 1, date(2026, 7, 9))
		self.assertEqual(result, {"eligible": 2, "queued": 1, "skipped": 1, "failed": 0})

	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.email_block_reason", return_value="Outbound email disabled.")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_log_available", return_value=True)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.outbound_email_enabled", return_value=False)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_invoice_reminders_enabled", return_value=True)
	def test_scheduler_obeys_environment_email_block(self, _mock_enabled, _mock_outbound, _mock_log, _mock_reason):
		result = run_overdue_invoice_reminders(today=date(2026, 7, 9))
		self.assertTrue(result["skipped"])


class TestOverdueReminderWorker(TestCase):
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._mark_notification_failed")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_outstanding_amount", return_value=0)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.frappe.get_doc")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_was_sent", return_value=False)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_invoice_reminders_enabled", return_value=True)
	def test_worker_revalidates_paid_invoice_before_sending(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_sent,
		mock_get_doc,
		_mock_outstanding,
		mock_failed,
	):
		mock_get_doc.return_value = invoice()
		result = send_overdue_invoice_reminder_job(
			"INV-001",
			1,
			"event-key",
			"LOG-001",
			today=date(2026, 7, 9),
		)
		self.assertTrue(result["skipped"])
		mock_failed.assert_called_once()

	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._mark_notification_sent")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._refresh_notification_log")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_pdf_attachment", return_value={"fname": "invoice.pdf"})
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.sendmail_or_skip")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.get_invoice_settings")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_reminder_subject", return_value="Payment reminder")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_reminder_email_message", return_value="Message")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_recipient")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_outstanding_amount", return_value=120)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.frappe.get_doc")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_was_sent", return_value=False)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.overdue_invoice_reminders_enabled", return_value=True)
	def test_worker_sends_individual_invoice_email_with_pdf_and_reply_to(
		self,
		_mock_enabled,
		_mock_outbound,
		_mock_sent_before,
		mock_get_doc,
		_mock_outstanding,
		mock_recipient,
		_mock_message,
		_mock_subject,
		mock_settings,
		mock_sendmail,
		_mock_pdf,
		_mock_refresh,
		mock_mark_sent,
	):
		mock_get_doc.return_value = invoice()
		mock_recipient.return_value = {"email": "parent@example.com", "for_user": "parent@example.com"}
		mock_settings.return_value = {"school_email": "queenslandartschool@gmail.com"}
		mock_sendmail.return_value = None

		result = send_overdue_invoice_reminder_job(
			"ACC-SINV-2026-00001",
			1,
			"event-key",
			"LOG-001",
			today=date(2026, 7, 9),
		)

		self.assertTrue(result["sent"])
		self.assertEqual(mock_sendmail.call_args.kwargs["recipients"], ["parent@example.com"])
		self.assertEqual(mock_sendmail.call_args.kwargs["reply_to"], "queenslandartschool@gmail.com")
		self.assertEqual(mock_sendmail.call_args.kwargs["attachments"], [{"fname": "invoice.pdf"}])
		mock_mark_sent.assert_called_once_with("LOG-001")


class TestOverdueReminderContent(TestCase):
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_email_portal_action", return_value="<a>View invoice</a>")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_email_bank_details", return_value="<div>Bank details</div>")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_email_greeting", return_value="Hi Parent,")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._invoice_portal_links_enabled", return_value=True)
	@patch(
		"qas_custom.modules.notifications.invoice_overdue_reminders.parent_portal_invoice_link",
		return_value="https://portal.example.com/invoices",
	)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.get_invoice_settings")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.build_parent_invoice_context")
	def test_email_contains_due_date_outstanding_payment_guidance_and_portal_link(
		self,
		mock_context,
		mock_settings,
		_mock_link,
		_mock_links,
		_mock_greeting,
		_mock_bank,
		_mock_portal,
		_mock_translate,
	):
		mock_context.return_value = {
			"school_name": "Queensland Art School",
			"due_date": "8 July 2026",
			"items": [],
		}
		mock_settings.return_value = {
			"school_email": "queenslandartschool@gmail.com",
			"school_phone": "0422 688 077",
		}

		message = overdue_reminder_email_message(invoice(), days_overdue=1, outstanding_amount=120)

		self.assertIn("1 day overdue", message)
		self.assertIn("8 July 2026", message)
		self.assertIn("AUD $120.00", message)
		self.assertIn("ACC-SINV-2026-00001", message)
		self.assertIn("Bank details", message)
		self.assertIn("View invoice", message)
		self.assertIn("already paid", message)
		self.assertEqual(overdue_reminder_subject(invoice()), "Payment reminder: Invoice ACC-SINV-2026-00001 is overdue")


class TestOverdueReminderReservation(TestCase):
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders.frappe.new_doc")
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_event_exists", return_value=False)
	@patch("qas_custom.modules.notifications.invoice_overdue_reminders._notification_log_available", return_value=True)
	def test_reservation_uses_event_key_and_invoice_reference(
		self,
		_mock_available,
		_mock_exists,
		mock_new_doc,
	):
		from qas_custom.modules.notifications.invoice_overdue_reminders import _reserve_notification_event

		log = SimpleNamespace(
			meta=SimpleNamespace(has_field=lambda fieldname: fieldname in {"for_user", "event_key", "recipient_email"}),
			flags=SimpleNamespace(),
			insert=Mock(),
			name="LOG-001",
		)
		mock_new_doc.return_value = log
		cache = SimpleNamespace(lock=Mock(return_value=nullcontext()))
		with patch("qas_custom.modules.notifications.invoice_overdue_reminders.frappe.cache", cache), patch(
			"qas_custom.modules.notifications.invoice_overdue_reminders.frappe.session",
			SimpleNamespace(user="Administrator"),
		):
			name = _reserve_notification_event(
				"invoice_overdue_reminder:INV-001:2026-07-08:1",
				invoice(name="INV-001"),
				{"email": "parent@example.com"},
				"Subject",
				"Message",
			)
		self.assertEqual(name, "LOG-001")
		self.assertEqual(log.document_type, "Sales Invoice")
		self.assertEqual(log.document_name, "INV-001")
		self.assertEqual(log.event_key, "invoice_overdue_reminder:INV-001:2026-07-08:1")
		log.insert.assert_called_once_with(ignore_permissions=True)
