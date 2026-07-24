from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.commands import (
	_invoice_email_message,
	_receipt_email_message,
)


def invoice_context():
	return {
		"invoice": "SINV-0001",
		"school_name": "Queensland Art School",
		"recipient_name": "Taylor",
		"due_date": "25 July 2026",
		"total": 450,
		"store_credit_applied": 0,
		"payable_amount": 450,
		"accepted_payment_methods": "Bank transfer",
		"invoice_message": "",
		"invoice_link": "https://portal.example.com/invoices?invoice=SINV-0001",
		"items": [],
		"adjustments": [],
	}


class TestParentInvoicePortalActions(TestCase):
	@patch("qas_custom.modules.notifications.commands._school_identity_email_html", return_value="")
	@patch("qas_custom.modules.notifications.commands._invoice_email_bank_details", return_value="")
	@patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
	def test_invoice_email_always_builds_and_displays_portal_action(
		self,
		mock_context,
		_mock_bank,
		_mock_identity,
	):
		mock_context.return_value = invoice_context()
		invoice = SimpleNamespace(name="SINV-0001")

		html = _invoice_email_message(
			invoice,
			event="approved",
			store_credit_applied=0,
			payable_amount=450,
			payment_link="https://portal.example.com/invoices?invoice=SINV-0001",
		)

		self.assertIn("View Invoice in Parent Portal", html)
		self.assertIn("https://portal.example.com/invoices?invoice=SINV-0001", html)
		self.assertTrue(mock_context.call_args.kwargs["include_portal_link"])

	@patch("qas_custom.modules.notifications.commands._school_identity_email_html", return_value="")
	@patch(
		"qas_custom.modules.notifications.commands.parent_portal_invoice_link",
		return_value="https://portal.example.com/invoices?invoice=SINV-0001",
	)
	@patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
	def test_receipt_email_always_displays_invoice_portal_action(self, mock_context, _mock_link, _mock_identity):
		mock_context.return_value = invoice_context()
		invoice = SimpleNamespace(name="SINV-0001")

		html = _receipt_email_message(
			invoice,
			{"store_credit_applied": 0},
			{
				"payment_date_display": "25 July 2026",
				"payment_method": "EFTPOS",
				"payment_amount": 450,
				"remaining_amount": 0,
				"receipt_reference": "PAY-001",
			},
		)

		self.assertIn("View Invoice in Parent Portal", html)
		self.assertIn("https://portal.example.com/invoices?invoice=SINV-0001", html)
		self.assertTrue(mock_context.call_args.kwargs["include_portal_link"])
