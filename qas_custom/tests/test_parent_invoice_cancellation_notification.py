from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.notifications.commands import (
	_invoice_cancellation_email_message,
	enqueue_parent_invoice_cancellation_notification,
)


class TestParentInvoiceCancellationNotification(TestCase):
	@patch("qas_custom.modules.notifications.commands._school_identity_email_html", return_value="")
	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
	def test_cancellation_email_has_no_payment_request_or_pdf_language(self, mock_context, _mock_translate, _mock_identity):
		mock_context.return_value = {
			"school_name": "Queensland Art School",
			"recipient_name": "Taylor",
		}
		invoice = SimpleNamespace(name="SINV-0001")

		message = _invoice_cancellation_email_message(invoice, reason="Family withdrew")

		self.assertIn("Invoice <strong>SINV-0001</strong> has been cancelled", message)
		self.assertIn("Family withdrew", message)
		self.assertNotIn("arrange payment", message.lower())
		self.assertNotIn("attached pdf", message.lower())

	@patch("qas_custom.modules.notifications.commands.frappe.enqueue")
	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.commands.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.commands._mark_notification_queued")
	@patch("qas_custom.modules.notifications.commands._create_notification_log", return_value="NOTIF-001")
	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_email_message", return_value="message")
	@patch("qas_custom.modules.notifications.commands._invoice_recipient")
	def test_enqueue_uses_async_job_without_attachment(self, mock_recipient, _mock_message, _mock_log, _mock_queued, _mock_enabled, _mock_translate, mock_enqueue):
		mock_recipient.return_value = {"email": "parent@example.com", "for_user": None}
		invoice = SimpleNamespace(name="SINV-0001")

		result = enqueue_parent_invoice_cancellation_notification(invoice, reason="Family withdrew")

		self.assertTrue(result["queued"])
		mock_enqueue.assert_called_once()
		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["invoice"], "SINV-0001")
		self.assertEqual(kwargs["reason"], "Family withdrew")
		self.assertNotIn("attachments", kwargs)
