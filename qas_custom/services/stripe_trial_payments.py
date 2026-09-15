"""Restricted trial-invoice Checkout pilot. No public rollout switch in this release."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode, urlparse

import frappe
import requests
from frappe.utils import cint, get_datetime, get_url, now_datetime

from qas_custom.modules.billing.store_credit import get_invoice_payable_amount
from qas_custom.utils.environment import payment_mutations_enabled

PILOT_USER = "rogerxu130@gmail.com"
SETTINGS = "QAS Stripe Settings"
PAYMENT = "QAS Stripe Payment"
TRIAL_SOURCES = {"Trial Inquiry", "Replacement Trial Inquiry"}


def cents(amount):
	return int((Decimal(str(amount or 0)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def settings():
	if not frappe.db.exists("DocType", SETTINGS):
		return None
	return frappe.get_single(SETTINGS)


def secret(config, suffix):
	return config.get_password(f"{config.mode.lower()}_{suffix}", raise_exception=False) or ""


def configured(config):
	return bool(config and cint(config.enabled) and config.mode in ("Test", "Live")
		and secret(config, 'secret_key') and secret(config, 'webhook_secret')
		and config.get_password('signing_secret', raise_exception=False)
		and config.company and config.clearing_account and config.mode_of_payment
		and payment_mutations_enabled())


def pilot_parent(doc):
	"""Use the invoice's actual account and owner, never request-supplied email."""
	parent_name = doc.get('parent')
	if not parent_name:
		parents = frappe.get_all('Parent', filters={'customer': doc.get('customer')}, pluck='name', limit_page_length=2)
		if len(parents) != 1:
			return None
		parent_name = parents[0]
	parent = frappe.get_doc('Parent', parent_name)
	if (str(parent.get('linked_user') or '').strip().lower() != PILOT_USER
		or parent.get('customer') != doc.get('customer') or parent.get('status') != 'Active'):
		return None
	if not cint(frappe.db.get_value('User', parent.linked_user, 'enabled')):
		return None
	return parent


def is_trial(doc):
	# Explicit trial source or authoritative trial-invoice link, never text matching.
	if doc.get('source_type') in TRIAL_SOURCES:
		return True
	return bool(frappe.db.exists('Inquiry', {'trial_invoice': doc.name, 'inquiry_type': 'Trial Lesson'}))


def eligible(doc, config):
	return bool(configured(config) and doc.get('company') == config.company
		and cint(doc.docstatus) == 1 and not cint(doc.get('is_return'))
		and doc.get('currency') == 'AUD' and is_trial(doc) and pilot_parent(doc)
		and cents(get_invoice_payable_amount(doc)) > 0)


def support_request():
	request = getattr(frappe.local, 'request', None)
	return bool(request and request.headers.get('X-QAS-Support-View-Token'))


def sign_token(invoice, parent, config, expires=None):
	payload = json.dumps([invoice, parent, config.mode, expires or int(time.time()) + 30 * 86400], separators=(',', ':'))
	encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
	digest = hmac.new(config.get_password('signing_secret').encode(), encoded.encode(), hashlib.sha256).hexdigest()
	return encoded + '.' + digest


def decode_token(token, config):
	try:
		encoded, signature = str(token or '').split('.')
		if len(encoded) > 2000:
			raise ValueError()
		expected = hmac.new(config.get_password('signing_secret').encode(), encoded.encode(), hashlib.sha256).hexdigest()
		if not hmac.compare_digest(signature, expected):
			raise ValueError()
		invoice, parent, mode, expires = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
		if mode != config.mode or int(expires) < int(time.time()):
			raise ValueError()
		return invoice, parent
	except (ValueError, TypeError, KeyError, UnicodeError):
		frappe.throw('Payment link is invalid or expired.', frappe.PermissionError)


def payment_url(doc):
	config = settings()
	if support_request() or not eligible(doc, config):
		return ''
	from qas_custom.modules.billing.presentation import parent_portal_invoice_link
	portal = parent_portal_invoice_link(doc.name).split('/invoices', 1)[0]
	return portal + '/trial-payment#' + urlencode({'token': sign_token(doc.name, pilot_parent(doc).name, config)})


def authorize(token):
	config = settings()
	if support_request() or not configured(config):
		frappe.throw('Online payment is not available.', frappe.PermissionError)
	invoice, parent_name = decode_token(token, config)
	doc = frappe.get_doc('Sales Invoice', invoice)
	parent = pilot_parent(doc)
	if not parent or parent.name != parent_name or not is_trial(doc) or doc.company != config.company:
		frappe.throw('Online payment is not available.', frappe.PermissionError)
	return doc, config


def summary(token):
	from frappe.sessions import get_csrf_token
	doc, config = authorize(token)
	latest = frappe.get_all(PAYMENT, filters={'invoice': doc.name, 'mode': config.mode},
		fields=['status'], order_by='creation desc', limit_page_length=1)
	state = latest[0].status if latest else ''
	can_pay = eligible(doc, config) and state not in ('Needs Review', 'Retry', 'Test Paid', 'Paid')
	return {'invoice': doc.name, 'amount': max(0, cents(get_invoice_payable_amount(doc))) / 100,
		'currency': doc.currency, 'mode': config.mode, 'can_pay': can_pay,
		'status': state, 'settled': cint(doc.docstatus) == 1 and cents(get_invoice_payable_amount(doc)) <= 0,
		'csrf_token': get_csrf_token()}


def stripe_request(config, method, path, data=None, idempotency_key=None):
	headers = {'Stripe-Version': '2024-06-20'}
	if idempotency_key:
		headers['Idempotency-Key'] = idempotency_key
	response = requests.request(method, 'https://api.stripe.com/v1/' + path,
		auth=(secret(config, 'secret_key'), ''), headers=headers, data=data, timeout=(5, 20))
	if response.status_code >= 400:
		# Do not include Stripe response bodies or credentials in client-facing errors.
		raise RuntimeError(f'Stripe request failed (HTTP {response.status_code}).')
	return response.json()


def start_checkout(token):
	doc, config = authorize(token)
	validate_accounts(config)
	# Redis serialises our external calls, DB locks coordinate accounting writes.
	with frappe.cache.lock('qas-stripe:' + doc.name, timeout=90, blocking_timeout=5):
		frappe.db.sql('select name from `tabSales Invoice` where name=%s for update', doc.name)
		doc.reload()
		if not eligible(doc, config):
			frappe.throw('This invoice is no longer available for online payment.')
		amount = cents(get_invoice_payable_amount(doc))
		rows = frappe.get_all(PAYMENT, filters={'invoice': doc.name, 'mode': config.mode,
			'status': ['!=', 'Expired']}, pluck='name', order_by='creation desc', limit_page_length=1)
		attempt = frappe.get_doc(PAYMENT, rows[0]) if rows else None
		if attempt and attempt.status in ('Paid', 'Test Paid', 'Needs Review', 'Retry'):
			frappe.throw('A payment has already been received or requires review. Please refresh the invoice.')
		if attempt and attempt.checkout_session:
			session = stripe_request(config, 'GET', 'checkout/sessions/' + attempt.checkout_session)
			if session.get('payment_status') == 'paid' or session.get('status') == 'complete':
				frappe.throw('Payment is being confirmed. Please refresh shortly.')
			if session.get('status') == 'open' and attempt.amount_cents == amount:
				return {'url': checked_checkout_url(session.get('url'))}
			if session.get('status') == 'open':
				stripe_request(config, 'POST', 'checkout/sessions/' + attempt.checkout_session + '/expire')
			attempt.status = 'Expired'
			attempt.save(ignore_permissions=True)
			attempt = None
		if attempt and (now_datetime() - get_datetime(attempt.creation)).total_seconds() > 23 * 3600:
			attempt.status = 'Needs Review'
			attempt.last_error = 'Uncertain Checkout creation is older than 23 hours; reconcile in Stripe before retrying.'
			attempt.save(ignore_permissions=True)
			frappe.db.commit()
			frappe.throw('Payment requires staff review.')
		if not attempt:
			attempt = frappe.get_doc({'doctype': PAYMENT, 'invoice': doc.name, 'mode': config.mode,
				'amount_cents': amount, 'currency': 'aud', 'status': 'Pending'}).insert(ignore_permissions=True)
		# Persist idempotency identity BEFORE network access. Retry uses exactly the same payload.
		frappe.db.commit()
		from qas_custom.modules.billing.presentation import parent_portal_invoice_link
		portal = parent_portal_invoice_link(doc.name).split('/invoices', 1)[0]
		# Stable return URL allows retry after a timeout with the same idempotency key.
		return_url = portal + '/trial-payment?attempt=' + attempt.name
		data = {'mode': 'payment', 'payment_method_types[0]': 'card',
			'line_items[0][price_data][currency]': 'aud',
			'line_items[0][price_data][unit_amount]': attempt.amount_cents,
			'line_items[0][price_data][product_data][name]': 'QAS trial invoice ' + doc.name,
			'line_items[0][quantity]': 1, 'client_reference_id': attempt.name,
			'metadata[qas_attempt]': attempt.name, 'metadata[qas_invoice]': doc.name,
			'payment_intent_data[metadata][qas_attempt]': attempt.name,
			'success_url': return_url, 'cancel_url': return_url}
		session = stripe_request(config, 'POST', 'checkout/sessions', data, 'qas-trial-' + attempt.name)
		attempt.reload()
		if attempt.status in ('Paid', 'Test Paid', 'Needs Review', 'Retry') or attempt.payment_entry:
			frappe.throw('Payment is already being confirmed. Please refresh the invoice.')
		attempt.checkout_session = session['id']
		attempt.checkout_url = checked_checkout_url(session['url'])
		attempt.status = 'Open'
		attempt.save(ignore_permissions=True)
		frappe.db.commit()
		# If balance changed during an uncertain creation, never send customer to stale Checkout.
		doc.reload()
		if not eligible(doc, config) or cents(get_invoice_payable_amount(doc)) != attempt.amount_cents:
			stripe_request(config, 'POST', 'checkout/sessions/' + attempt.checkout_session + '/expire')
			attempt.status = 'Expired'
			attempt.save(ignore_permissions=True)
			frappe.db.commit()
			frappe.throw('Invoice changed. Refresh and try again.')
		return {'url': attempt.checkout_url}


def checked_checkout_url(url):
	parsed = urlparse(str(url or ''))
	if parsed.scheme != 'https' or parsed.hostname != 'checkout.stripe.com':
		raise ValueError('Invalid Stripe Checkout URL.')
	return url


def verify_event(body, signature, signing_secret, now=None):
	try:
		parts = [part.split('=', 1) for part in (signature or '').split(',')]
		timestamp = next(value for key, value in parts if key == 't')
		if abs((now if now is not None else time.time()) - int(timestamp)) > 300:
			raise ValueError('Expired signature')
		expected = hmac.new(signing_secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
		if not any(key == 'v1' and hmac.compare_digest(value, expected) for key, value in parts):
			raise ValueError('Invalid signature')
		return json.loads(body)
	except (ValueError, StopIteration, TypeError):
		raise ValueError('Invalid Stripe webhook signature') from None


def webhook():
	config = settings()
	if not config:
		frappe.throw('Stripe is not configured.')
	body = frappe.request.get_data()
	# Accept either configured environment, even after mode changes, to finish in-flight payments.
	event = None
	for mode in ('Test', 'Live'):
		config.mode = mode
		key = secret(config, 'webhook_secret')
		if not key:
			continue
		try:
			event = verify_event(body, frappe.get_request_header('Stripe-Signature'), key)
			break
		except ValueError:
			continue
	if not event or bool(event.get('livemode')) != (config.mode == 'Live'):
		frappe.local.response.http_status_code = 400
		return {'ok': False}
	if event.get('type') not in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
		return {'ok': True}
	session_id = (event.get('data', {}).get('object') or {}).get('id', '')
	if not str(session_id).startswith('cs_'):
		frappe.local.response.http_status_code = 400
		return {'ok': False}
	session = stripe_request(config, 'GET', 'checkout/sessions/' + session_id)
	attempt_name = (session.get('metadata') or {}).get('qas_attempt')
	if not attempt_name or not frappe.db.exists(PAYMENT, attempt_name):
		return {'ok': True}  # Other applications may share the Stripe account.
	try:
		with frappe.cache.lock('qas-stripe-event:' + attempt_name, timeout=90, blocking_timeout=5):
			return settle(session, config, event['id'])
	except Exception:
		frappe.db.rollback()
		# Keep a durable review queue without leaking raw gateway errors.
		frappe.db.set_value(PAYMENT, attempt_name, {'status': 'Retry', 'last_event': event['id'],
			'last_error': 'Payment confirmation failed. Retry this event from Stripe after resolving configuration/accounting errors.'})
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), 'QAS Stripe payment confirmation failed')
		frappe.local.response.http_status_code = 500
		return {'ok': False}


def settle(session, config, event_id):
	attempt_name = session['metadata']['qas_attempt']
	frappe.db.sql('select name from `tabQAS Stripe Payment` where name=%s for update', attempt_name)
	attempt = frappe.get_doc(PAYMENT, attempt_name)
	if (attempt.mode != config.mode or bool(session.get('livemode')) != (config.mode == 'Live')
		or session.get('client_reference_id') != attempt.name
		or session['metadata'].get('qas_invoice') != attempt.invoice
		or (attempt.checkout_session and attempt.checkout_session != session['id'])
		or session.get('amount_total') != attempt.amount_cents or session.get('currency') != attempt.currency):
		frappe.throw('Payment identity or amount mismatch.')
	if session.get('payment_status') != 'paid':
		return {'ok': True}
	if attempt.status in ('Paid', 'Test Paid') or attempt.payment_entry:
		return {'ok': True}
	attempt.checkout_session = session['id']
	attempt.payment_intent = session.get('payment_intent')
	attempt.last_event = event_id
	if not attempt.payment_intent:
		frappe.throw('Missing payment reference.')
	frappe.db.sql('select name from `tabSales Invoice` where name=%s for update', attempt.invoice)
	doc = frappe.get_doc('Sales Invoice', attempt.invoice)
	# Rollout gating remains enforced, but disabling checkout must not discard money already received.
	if (not pilot_parent(doc) or not is_trial(doc) or cint(doc.docstatus) != 1 or cint(doc.get('is_return'))
		or doc.company != config.company or doc.currency != 'AUD'
		or cents(get_invoice_payable_amount(doc)) != attempt.amount_cents):
		attempt.status = 'Needs Review'
		attempt.last_error = 'Invoice owner, eligibility or balance changed after Checkout. Reconcile the Stripe payment manually.'
	elif config.mode == 'Test':
		attempt.last_error = ''
		attempt.status = 'Test Paid'  # Never settle a real invoice with simulated funds.
	else:
		if not payment_mutations_enabled():
			frappe.throw('Payment posting is disabled in this environment.')
		entry = create_payment_entry(doc, attempt, config)
		attempt.payment_entry = entry.name
		attempt.status = 'Paid'
		attempt.last_error = ''
		doc.add_comment('Comment', 'Stripe payment recorded: ' + entry.name)
		from qas_custom.services.school_admin import _enqueue_paid_receipt
		receipt = _enqueue_paid_receipt(frappe.get_doc('Sales Invoice', doc.name), payment_entry=entry, source='stripe')
		attempt.receipt_queued = cint((receipt or {}).get('queued'))
	attempt.save(ignore_permissions=True)
	frappe.db.commit()
	return {'ok': True}


def create_payment_entry(doc, attempt, config):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
	from qas_custom.modules.billing.store_credit import (
		sync_invoice_store_credit_snapshot, get_invoice_store_credit_applied,
		has_invoice_store_credit_journal_entry, ensure_store_credit_journal_entry,
	)
	validate_accounts(config)
	original_user = frappe.session.user
	try:
		frappe.set_user('Administrator')
		credit = get_invoice_store_credit_applied(doc.name)
		if credit > 0 and not has_invoice_store_credit_journal_entry(doc.name):
			ensure_store_credit_journal_entry(doc, amount=credit)
			doc.reload()
		entry = get_payment_entry('Sales Invoice', doc.name, bank_account=config.clearing_account,
			bank_amount=attempt.amount_cents / 100, ignore_permissions=True)
		entry.mode_of_payment = config.mode_of_payment
		entry.reference_no = attempt.payment_intent
		entry.reference_date = now_datetime().date()
		entry.remarks = 'Stripe trial invoice payment ' + attempt.payment_intent
		entry.paid_amount = entry.received_amount = attempt.amount_cents / 100
		for reference in entry.references:
			reference.allocated_amount = attempt.amount_cents / 100 if reference.reference_name == doc.name else 0
		entry.insert(ignore_permissions=True)
		entry.submit()
		sync_invoice_store_credit_snapshot(doc.name)
		return entry
	finally:
		frappe.set_user(original_user)


def validate_accounts(config):
	account = frappe.get_doc('Account', config.clearing_account)
	if (account.company != config.company or account.account_currency != 'AUD'
		or cint(account.is_group) or cint(account.disabled) or account.account_type not in ('Bank', 'Cash')):
		frappe.throw('Choose an active AUD bank/cash clearing account for the configured company.')
	if not frappe.db.exists('Mode of Payment', config.mode_of_payment):
		frappe.throw('Choose a valid mode of payment.')
