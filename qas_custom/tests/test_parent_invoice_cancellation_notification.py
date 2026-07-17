from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.commands import (
	_invoice_cancellation_email_message,
	_invoice_cancellation_pdf_attachment,
	_invoice_cancellation_pdf_html,
	enqueue_parent_invoice_cancellation_notification,
	render_parent_invoice_cancellation_pdf,
	send_parent_invoice_cancellation_notification,
)


class TestParentInvoiceCancellationNotification(TestCase):
	@patch("qas_custom.modules.notifications.commands._school_identity_email_html", return_value="")
	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
	def test_cancellation_email_includes_invoice_details_and_no_payment_notice(self, mock_context, _mock_translate, _mock_identity):
		mock_context.return_value = {
			"school_name": "Queensland Art School",
			"recipient_name": "Taylor",
			"total": 450,
			"items": [
				{
					"student": "Alex Student",
					"description": "Term 3 Creative Art",
					"amount": 450,
				}
			],
		}
		invoice = SimpleNamespace(name="SINV-0001")

		message = _invoice_cancellation_email_message(invoice, reason="Family withdrew")

		self.assertIn("Invoice <strong>SINV-0001</strong> has been cancelled", message)
		self.assertIn("This invoice has been cancelled. No payment is required.", message)
		self.assertIn("Original invoice total", message)
		self.assertIn("AUD $450.00", message)
		self.assertIn("Alex Student", message)
		self.assertIn("Term 3 Creative Art", message)
		self.assertIn("Family withdrew", message)
		self.assertIn("cancelled copy of the invoice is attached", message)
		self.assertNotIn("arrange payment", message.lower())

	@patch("qas_custom.modules.notifications.commands._school_identity_pdf_html", return_value="")
	def test_cancelled_pdf_html_excludes_payable_invoice_fields(self, _mock_identity):
		html = _invoice_cancellation_pdf_html(
			{
				"invoice": "SINV-0001",
				"school_name": "Queensland Art School",
				"recipient_name": "Taylor Parent",
				"total": 450,
				"cancellation_reason": "Family withdrew",
				"items": [
					{
						"student": "Alex Student",
						"description": "Term 3 Creative Art",
						"qty": 2,
						"rate": 225,
						"amount": 450,
					}
				],
			}
		)

		self.assertIn("CANCELLED", html)
		self.assertIn("SINV-0001", html)
		self.assertIn("Taylor Parent", html)
		self.assertIn("Alex Student", html)
		self.assertIn("Term 3 Creative Art", html)
		self.assertIn("AUD $450.00", html)
		self.assertIn("Family withdrew", html)
		self.assertIn("No payment is required", html)
		self.assertNotIn("Due date", html)
		self.assertNotIn("Amount payable", html)
		self.assertNotIn("Bank transfer", html)
		self.assertNotIn("View invoice", html)

	@patch("qas_custom.modules.notifications.commands.get_pdf", return_value=b"cancelled-pdf")
	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_pdf_html", return_value="cancelled-html")
	@patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
	@patch("qas_custom.modules.notifications.commands.frappe.get_doc")
	def test_cancelled_pdf_renderer_uses_no_portal_context(self, mock_get_doc, mock_context, mock_html, mock_get_pdf):
		invoice = SimpleNamespace(name="SINV-0001")
		mock_get_doc.return_value = invoice
		context = {"invoice": "SINV-0001"}
		mock_context.return_value = context

		pdf = render_parent_invoice_cancellation_pdf("SINV-0001", reason="Family withdrew")

		self.assertEqual(pdf, b"cancelled-pdf")
		mock_context.assert_called_once_with(invoice, include_portal_link=False)
		self.assertEqual(context["cancellation_reason"], "Family withdrew")
		mock_html.assert_called_once_with(context)
		mock_get_pdf.assert_called_once_with("cancelled-html")

	@patch("qas_custom.modules.notifications.commands.save_file")
	@patch("qas_custom.modules.notifications.commands.render_parent_invoice_cancellation_pdf", return_value=b"cancelled-pdf")
	def test_cancelled_pdf_attachment_is_private_and_clearly_named(self, mock_render, mock_save_file):
		mock_save_file.return_value = SimpleNamespace(name="FILE-001")

		attachment = _invoice_cancellation_pdf_attachment("SINV-0001", reason="Family withdrew")

		self.assertEqual(attachment, {"fid": "FILE-001"})
		mock_render.assert_called_once_with("SINV-0001", reason="Family withdrew")
		mock_save_file.assert_called_once_with(
			"SINV-0001-CANCELLED.pdf",
			b"cancelled-pdf",
			"Sales Invoice",
			"SINV-0001",
			is_private=1,
		)

	@patch("qas_custom.modules.notifications.commands._mark_notification_sent")
	@patch("qas_custom.modules.notifications.commands.sendmail_or_skip", return_value={})
	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_pdf_attachment", return_value={"fid": "FILE-001"})
	@patch("qas_custom.modules.notifications.commands._create_notification_log", return_value="NOTIF-001")
	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_email_message", return_value="message")
	@patch("qas_custom.modules.notifications.commands._invoice_recipient")
	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	def test_send_attaches_cancelled_pdf(
		self,
		_mock_translate,
		mock_recipient,
		_mock_message,
		_mock_log,
		mock_attachment,
		mock_sendmail,
		mock_mark_sent,
	):
		mock_recipient.return_value = {"email": "parent@example.com", "for_user": None}
		invoice = SimpleNamespace(name="SINV-0001")

		result = send_parent_invoice_cancellation_notification(invoice, reason="Family withdrew")

		self.assertTrue(result["sent"])
		mock_attachment.assert_called_once_with("SINV-0001", reason="Family withdrew")
		self.assertEqual(mock_sendmail.call_args.kwargs["attachments"], [{"fid": "FILE-001"}])
		mock_mark_sent.assert_called_once_with("NOTIF-001")

	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_pdf_attachment")
	@patch("qas_custom.modules.notifications.commands.frappe.enqueue")
	@patch("qas_custom.modules.notifications.commands._", side_effect=lambda value: value)
	@patch("qas_custom.modules.notifications.commands.outbound_email_enabled", return_value=True)
	@patch("qas_custom.modules.notifications.commands._mark_notification_queued")
	@patch("qas_custom.modules.notifications.commands._create_notification_log", return_value="NOTIF-001")
	@patch("qas_custom.modules.notifications.commands._invoice_cancellation_email_message", return_value="message")
	@patch("qas_custom.modules.notifications.commands._invoice_recipient")
	def test_enqueue_uses_async_job_without_rendering_attachment(
		self,
		mock_recipient,
		_mock_message,
		_mock_log,
		_mock_queued,
		_mock_enabled,
		_mock_translate,
		mock_enqueue,
		mock_attachment,
	):
		mock_recipient.return_value = {"email": "parent@example.com", "for_user": None}
		invoice = SimpleNamespace(name="SINV-0001")

		result = enqueue_parent_invoice_cancellation_notification(invoice, reason="Family withdrew")

		self.assertTrue(result["queued"])
		mock_enqueue.assert_called_once()
		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["invoice"], "SINV-0001")
		self.assertEqual(kwargs["reason"], "Family withdrew")
		self.assertNotIn("attachments", kwargs)
		mock_attachment.assert_not_called()
