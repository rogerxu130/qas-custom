import hashlib
import hmac
import json
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from qas_custom.services import stripe_trial_payments as p
from qas_custom.modules.billing import invoice_naming
from frappe.model.naming import parse_naming_series


class StripeTrialPaymentTests(TestCase):
	def setUp(self):
		self.config = frappe._dict(mode='Test', enabled=1, company='QAS', clearing_account='Stripe', mode_of_payment='Stripe')
		self.config.get_password = lambda field, **kwargs: 'secret-for-test'
		self.invoice = frappe._dict(name='2600001', parent='P1', customer='C1', company='QAS', currency='AUD',
			docstatus=1, is_return=0, source_type='Trial Inquiry')

	def test_signature_valid_tampered_stale_and_rotation(self):
		body = b'{"id":"evt_1"}'
		sig = hmac.new(b'whsec_test', b'1000.' + body, hashlib.sha256).hexdigest()
		header = 't=1000,v1=old,v1=' + sig
		self.assertEqual(p.verify_event(body, header, 'whsec_test', now=1001)['id'], 'evt_1')
		for content, signature, now in [(body+b' ', header, 1001), (body, header, 1400), (body, 'bad', 1001)]:
			with self.assertRaises(ValueError): p.verify_event(content, signature, 'whsec_test', now=now)

	def test_scoped_token_tampering_expiry_and_mode(self):
		with patch.object(p.time, 'time', return_value=1000), patch.object(p.frappe, 'throw', side_effect=ValueError):
			token = p.sign_token('2600001', 'P1', self.config, expires=2000)
			self.assertEqual(p.decode_token(token, self.config), ('2600001', 'P1'))
			with self.assertRaises(ValueError): p.decode_token(token + 'x', self.config)
			with self.assertRaises(ValueError): p.decode_token(p.sign_token('2600001', 'P1', self.config, expires=900), self.config)
			self.config.mode = 'Live'
			with self.assertRaises(ValueError): p.decode_token(token, self.config)

	def test_pilot_uses_owner_not_contact_email_or_logged_in_operator(self):
		parent = frappe._dict(name='P1', linked_user=p.PILOT_USER, email='other@example.com', customer='C1', status='Active')
		with patch.object(p.frappe, 'get_doc', return_value=parent), patch.object(p.frappe, 'db', MagicMock()):
			p.frappe.db.get_value.return_value=1
			self.assertEqual(p.pilot_parent(self.invoice).name, 'P1')
			parent.linked_user='someone@example.com';parent.email=p.PILOT_USER
			self.assertIsNone(p.pilot_parent(self.invoice))
			parent.linked_user=p.PILOT_USER;parent.customer='C2'
			self.assertIsNone(p.pilot_parent(self.invoice))
			parent.customer='C1';parent.status='Inactive'
			self.assertIsNone(p.pilot_parent(self.invoice))

	def test_missing_owner_ambiguous_customer_denied(self):
		self.invoice.parent=None
		with patch.object(p.frappe, 'get_all', return_value=['P1', 'P2']):
			self.assertIsNone(p.pilot_parent(self.invoice))

	def test_eligibility_blocks_nonpilot_nontrial_draft_cancelled_credit_and_zero(self):
		with patch.object(p, 'configured', return_value=True), patch.object(p, 'pilot_parent', return_value=True) as parent, patch.object(p, 'is_trial', return_value=True) as trial, patch.object(p, 'get_invoice_payable_amount', return_value=30) as amount:
			self.assertTrue(p.eligible(self.invoice, self.config))
			for key, value in [('docstatus', 0), ('docstatus', 2), ('is_return', 1), ('currency', 'USD'), ('company', 'Other')]:
				doc = frappe._dict(self.invoice);doc[key] = value
				self.assertFalse(p.eligible(doc, self.config))
			parent.return_value=None;self.assertFalse(p.eligible(self.invoice,self.config));parent.return_value=True
			trial.return_value=False;self.assertFalse(p.eligible(self.invoice,self.config));trial.return_value=True
			amount.return_value=0;self.assertFalse(p.eligible(self.invoice,self.config))

	def test_link_not_generated_when_disabled_or_support_view(self):
		with patch.object(p, 'settings', return_value=None), patch.object(p, 'support_request', return_value=False):
			self.assertEqual(p.payment_url(self.invoice), '')
		with patch.object(p, 'settings', return_value=self.config), patch.object(p, 'support_request', return_value=True):
			self.assertEqual(p.payment_url(self.invoice), '')

	def test_amount_rounding_and_checkout_destination(self):
		self.assertEqual(p.cents('30.005'), 3001)
		self.assertEqual(p.cents('0.29'), 29)
		self.assertEqual(p.checked_checkout_url('https://checkout.stripe.com/c/pay/123'), 'https://checkout.stripe.com/c/pay/123')
		for url in ['http://checkout.stripe.com/pay', 'https://checkout.stripe.com.evil.test/pay', 'javascript:alert(1)']:
			with self.assertRaises(ValueError): p.checked_checkout_url(url)

	def session_and_attempt(self, mode='Test'):
		attempt = MagicMock()
		for key, value in dict(name='attempt1', invoice='2600001', mode=mode, status='Open', checkout_session='cs_1', amount_cents=3000, currency='aud', payment_entry=None).items(): setattr(attempt,key,value)
		session = dict(id='cs_1', livemode=mode=='Live', metadata={'qas_attempt':'attempt1','qas_invoice':'2600001'}, client_reference_id='attempt1', amount_total=3000, currency='aud', payment_status='paid', payment_intent='pi_1')
		return session,attempt

	def settle_context(self, attempt):
		def get_doc(doctype,name): return attempt if doctype == p.PAYMENT else self.invoice
		return patch.object(p.frappe, 'get_doc', side_effect=get_doc)

	def test_test_payment_and_duplicate_do_not_touch_accounting(self):
		session,attempt=self.session_and_attempt()
		with self.settle_context(attempt), patch.object(p.frappe,'db',MagicMock()), patch.object(p,'pilot_parent',return_value=True), patch.object(p,'is_trial',return_value=True), patch.object(p,'get_invoice_payable_amount',return_value=30), patch.object(p,'create_payment_entry') as create:
			p.settle(session,self.config,'evt1')
			self.assertEqual(attempt.status,'Test Paid')
			p.settle(session,self.config,'evt1')
			create.assert_not_called()
			self.assertEqual(attempt.save.call_count,1)

	def test_paid_before_callback_goes_to_review_not_second_payment(self):
		session,attempt=self.session_and_attempt('Live');self.config.mode='Live'
		with self.settle_context(attempt), patch.object(p.frappe,'db',MagicMock()), patch.object(p,'pilot_parent',return_value=True), patch.object(p,'is_trial',return_value=True), patch.object(p,'get_invoice_payable_amount',return_value=0), patch.object(p,'create_payment_entry') as create:
			p.settle(session,self.config,'evt1')
			self.assertEqual(attempt.status,'Needs Review');create.assert_not_called()

	def test_mismatched_session_amount_and_environment_rejected(self):
		for field,value in [('amount_total',1),('livemode',True),('currency','usd'),('client_reference_id','another')]:
			session,attempt=self.session_and_attempt();session[field]=value
			with self.settle_context(attempt),patch.object(p.frappe,'db',MagicMock()),patch.object(p.frappe,'throw',side_effect=ValueError):
				with self.assertRaises(ValueError):p.settle(session,self.config,'evt1')
				attempt.save.assert_not_called()

	def test_already_posted_payment_is_not_posted_again(self):
		session,attempt=self.session_and_attempt('Live');self.config.mode='Live';attempt.payment_entry='PE1'
		with self.settle_context(attempt),patch.object(p.frappe,'db',MagicMock()),patch.object(p,'create_payment_entry') as create:
			p.settle(session,self.config,'evt2');create.assert_not_called()


class InvoiceNamingTests(TestCase):
	def test_year_rollover_and_five_digit_padding(self):
		for year,expected in [(2026,'2600001'),(2027,'2700001')]:
			with patch('frappe.model.naming.now_datetime',return_value=datetime(year,1,1)):
				self.assertEqual(parse_naming_series('YY.#####',number_generator=lambda key,digits: str(1).zfill(digits)),expected)

	def test_new_amendment_keeps_audit_link_and_skips_existing_numbers(self):
		doc=MagicMock(name=None);doc.flags.name_set=False;doc.amended_from='ACC-SINV-2026-00188'
		with patch.object(invoice_naming.frappe,'flags',frappe._dict(in_import=False)),patch.object(invoice_naming,'make_autoname',side_effect=['2600001','2600002']),patch.object(invoice_naming.frappe,'db',MagicMock()):
			invoice_naming.frappe.db.exists.side_effect=[True,False]
			invoice_naming.name_invoice(doc)
			doc.set_new_name.assert_called_once_with(set_name='2600002')
			self.assertEqual(doc.amended_from,'ACC-SINV-2026-00188')

	def test_existing_name_set_and_explicit_import_not_renamed(self):
		for importing,name_set in [(True,False),(False,True)]:
			doc=MagicMock();doc.flags.name_set=name_set
			with patch.object(invoice_naming.frappe,'flags',frappe._dict(in_import=importing)),patch.object(invoice_naming,'make_autoname') as name:
				invoice_naming.name_invoice(doc);name.assert_not_called()

class CheckoutLifecycleTests(TestCase):
	def setUp(self):
		self.doc = MagicMock();self.doc.name = '2600001'
		self.config = frappe._dict(mode='Test')
		self.attempt = MagicMock()
		for key,value in dict(name='a1',invoice='2600001',mode='Test',status='Pending',checkout_session=None,amount_cents=3000,payment_entry=None,creation=datetime.now()).items():setattr(self.attempt,key,value)
		self.attempt.insert.return_value=self.attempt
		self.stack = __import__('contextlib').ExitStack()
		self.addCleanup(self.stack.close)
		for name,value in [('authorize',(self.doc,self.config)),('eligible',True),('get_invoice_payable_amount',30),('validate_accounts',None)]:
			self.stack.enter_context(patch.object(p,name,return_value=value))
		self.stack.enter_context(patch.object(p.frappe,'cache',MagicMock()))
		self.db=self.stack.enter_context(patch.object(p.frappe,'db',MagicMock()))
		self.rows=self.stack.enter_context(patch.object(p.frappe,'get_all',return_value=['a1']))
		self.stack.enter_context(patch.object(p.frappe,'get_doc',return_value=self.attempt))
		self.stack.enter_context(patch.object(p.frappe,'throw',side_effect=ValueError))
		self.stack.enter_context(patch('qas_custom.modules.billing.presentation.parent_portal_invoice_link',return_value='https://portal.example.com/invoices?invoice=2600001'))

	def test_timeout_retry_reuses_same_attempt_payload_and_idempotency_key(self):
		session={'id':'cs_1','url':'https://checkout.stripe.com/pay/1'}
		with patch.object(p,'stripe_request',side_effect=[TimeoutError(),session]) as request:
			with self.assertRaises(TimeoutError):p.start_checkout('token')
			result=p.start_checkout('token')
			self.assertEqual(request.call_args_list[0],request.call_args_list[1])
			self.assertEqual(request.call_args_list[1].args[-1],'qas-trial-a1')
			self.assertEqual(result['url'],session['url'])
			self.assertEqual(self.attempt.status,'Open')
			self.assertGreaterEqual(self.db.commit.call_count,2)

	def test_existing_open_checkout_is_reused_and_paid_session_is_blocked(self):
		self.attempt.status='Open';self.attempt.checkout_session='cs_1'
		with patch.object(p,'stripe_request',return_value={'status':'open','payment_status':'unpaid','url':'https://checkout.stripe.com/pay/1'}) as request:
			p.start_checkout('token');self.assertEqual(request.call_count,1)
		with patch.object(p,'stripe_request',return_value={'status':'complete','payment_status':'paid'}) as request:
			with self.assertRaises(ValueError):p.start_checkout('token')
			self.assertEqual(request.call_count,1)

	def test_uncertain_creation_expired_idempotency_requires_review(self):
		self.attempt.creation=datetime(2020,1,1)
		with patch.object(p,'stripe_request') as request:
			with self.assertRaises(ValueError):p.start_checkout('token')
			request.assert_not_called();self.assertEqual(self.attempt.status,'Needs Review')

	def test_payment_callback_during_creation_cannot_be_overwritten(self):
		def on_reload():self.attempt.status='Paid';self.attempt.payment_entry='PE1'
		self.attempt.reload.side_effect=on_reload
		with patch.object(p,'stripe_request',return_value={'id':'cs_1','url':'https://checkout.stripe.com/pay/1'}):
			with self.assertRaises(ValueError):p.start_checkout('token')
			self.assertEqual(self.attempt.status,'Paid')
			self.attempt.save.assert_not_called()


class InvoicePaymentEmailTests(TestCase):
	def test_pay_button_requires_explicit_pilot_url_and_balance(self):
		from qas_custom.modules.notifications.commands import _invoice_email_portal_action
		base={'invoice_link':'https://portal.example.com/invoices','payable_amount':30}
		self.assertNotIn('Pay online',_invoice_email_portal_action(base))
		pilot={**base,'online_payment_url':'https://portal.example.com/trial-payment#token=abc'}
		self.assertIn('Pay online',_invoice_email_portal_action(pilot))
		self.assertNotIn('Pay online',_invoice_email_portal_action({**pilot,'payable_amount':0}))

class LivePaymentPostingTests(TestCase):
	setUp = StripeTrialPaymentTests.setUp
	session_and_attempt = StripeTrialPaymentTests.session_and_attempt
	settle_context = StripeTrialPaymentTests.settle_context

	def test_live_confirmation_posts_once_and_queues_receipt(self):
		session,attempt=self.session_and_attempt('Live');self.config.mode='Live'
		self.invoice.add_comment=MagicMock()
		entry=frappe._dict(name='PE1')
		with self.settle_context(attempt),patch.object(p.frappe,'db',MagicMock()),patch.object(p,'pilot_parent',return_value=True),patch.object(p,'is_trial',return_value=True),patch.object(p,'get_invoice_payable_amount',return_value=30),patch.object(p,'payment_mutations_enabled',return_value=True),patch.object(p,'create_payment_entry',return_value=entry) as create,patch('qas_custom.services.school_admin._enqueue_paid_receipt',return_value={'queued':True}) as receipt:
			p.settle(session,self.config,'evt1')
			p.settle(session,self.config,'evt2')
			create.assert_called_once();receipt.assert_called_once()
			self.assertEqual(attempt.status,'Paid');self.assertEqual(attempt.payment_entry,'PE1')
			self.assertEqual(attempt.receipt_queued,1)
