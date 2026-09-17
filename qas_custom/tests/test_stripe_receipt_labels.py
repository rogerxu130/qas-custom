from unittest import TestCase
from unittest.mock import patch
import frappe
from qas_custom.modules.notifications import commands as n


class StripeReceiptLabels(TestCase):
	def test_subject_and_pdf_distinguish_simulated_funds(self):
		doc = frappe._dict(name='2600012', qas_stripe_test=1)
		self.assertTrue(n._receipt_email_subject(doc).startswith('[TEST]'))
		context = dict(invoice=doc.name, school_name='QAS', total=68, store_credit_applied=0,
			receipt=dict(is_test=True, payment_amount=68, remaining_amount=0))
		with patch.object(n, '_invoice_parent_lines', return_value=[]), patch.object(n, '_school_identity_pdf_html', return_value=''):
			html = n._receipt_pdf_html(context)
			self.assertIn('TEST Payment receipt', html)
			self.assertIn('No real money was collected', html)
			context['receipt']['is_test'] = False
			self.assertNotIn('TEST', n._receipt_pdf_html(context))

	def test_test_email_has_label_and_real_email_does_not(self):
		doc = frappe._dict(name='2600012', qas_stripe_test=1)
		context = dict(invoice=doc.name, school_name='QAS', total=68, store_credit_applied=0)
		with patch.object(n, 'parent_portal_invoice_link', return_value='https://portal.example.com/invoices'), patch.object(n, 'build_parent_invoice_context', return_value=context), patch.object(n, '_invoice_parent_lines', return_value=[]), patch.object(n, '_invoice_email_greeting', return_value='Hello'), patch.object(n, '_school_identity_email_html', return_value=''), patch.object(n, '_invoice_email_portal_action', return_value=''):
			message = n._receipt_email_message(doc, {'store_credit_applied': 0}, {})
			self.assertIn('TEST Payment receipt', message)
			self.assertIn('No real money was collected', message)
			doc.qas_stripe_test = 0
			self.assertNotIn('TEST', n._receipt_email_message(doc, {'store_credit_applied': 0}, {}))

	def test_admin_email_targets_configured_school_with_test_label(self):
		with patch.object(n, 'get_invoice_settings', return_value={'school_email': 'admin@example.com'}), patch.object(n, 'sendmail_or_skip') as send:
			n.notify_stripe_invoice_paid(frappe._dict(name='2600012'), frappe._dict(name='PE-TEST'), test=True)
			self.assertEqual(send.call_args.kwargs['recipients'], ['admin@example.com'])
			self.assertTrue(send.call_args.kwargs['subject'].startswith('[TEST]'))
			self.assertTrue(send.call_args.kwargs['delayed'])


	def test_simulated_payment_does_not_grant_credit_or_report_ad_conversion(self):
		from qas_custom.modules.billing.store_credit import grant_store_credit_bonus_for_payment_entry
		from qas_custom.services import google_conversion_sync as sync
		doc = frappe._dict(qas_stripe_test=1)
		self.assertTrue(grant_store_credit_bonus_for_payment_entry(doc)['skipped'])
		with patch.object(sync, '_capture_enabled') as enabled:
			sync.capture_payment_entry_submit(doc)
			enabled.assert_not_called()
