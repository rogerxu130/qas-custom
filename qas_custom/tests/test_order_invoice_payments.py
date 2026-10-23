"""Focused payment regression tests, independent of a running Frappe site.

Loads real service functions with fake database/gateway boundaries. Run with
python3 -m unittest discover -s qas_custom/tests -p test_order_invoice_payments.py
"""
import ast
import hashlib
import hmac
import json
import time
import unittest
from contextlib import nullcontext
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


class Doc(dict):
    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


def functions(path):
    tree = ast.parse(path.read_text())
    return compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)], type_ignores=[]), str(path), 'exec')


class OrderPayments(unittest.TestCase):
    def setUp(self):
        from urllib.parse import urlencode, urlparse
        base = Path(__file__).resolve().parents[1]
        self.frappe = ModuleType('frappe')
        self.frappe.throw = lambda message, *args: (_ for _ in ()).throw(ValueError(message))
        self.frappe.PermissionError = ValueError
        self.frappe.whitelist = lambda **kwargs: lambda function: function
        self.frappe.db = Mock()
        self.frappe.db.get_value.return_value = 1
        self.frappe.db.exists.return_value = False
        self.frappe.get_all = Mock(return_value=[])
        self.frappe.cache = SimpleNamespace(lock=lambda *a, **k: nullcontext())
        self.frappe.local = SimpleNamespace(request=None)
        self.order = Doc(name='OD-1', parent='P1', customer='C1', status='Ordered')
        self.parent = Doc(name='P1', customer='C1', status='Active', linked_user='parent@example.com')
        self.invoice = Doc(name='OI-1', parent='P1', customer='C1', company='QAS', currency='AUD', docstatus=1,
                           qas_invoice_type='Store Order', source_doctype='Store Order', source_document='OD-1',
                           outstanding_amount=129, qas_bank_account_name='QAS', qas_bank_bsb='123-456', qas_bank_account_number='12345678')
        self.invoice.reload = Mock()
        self.invoice.add_comment = Mock()
        self.config = Doc(company='QAS', mode='Live')
        self.state = dict(invoice_status='Issued', remaining_amount=129, invoices=[Doc(name='OI-1', docstatus=1, outstanding_amount=129)])
        self.frappe.get_doc = Mock(side_effect=lambda kind, name: {'Store Order': self.order, 'Parent': self.parent, 'Sales Invoice': self.invoice, 'QAS Stripe Payment': self.attempt}[kind])
        self.orders = ModuleType('qas_custom.services.order_invoices')
        self.orders.financials = Mock(side_effect=lambda doc: self.state)
        order_ns = dict(frappe=self.frappe, ORDER_TYPE='Store Order', Decimal=Decimal, ROUND_HALF_UP=ROUND_HALF_UP, cint=lambda v: int(v or 0), flt=lambda v: float(v or 0), _=lambda x: x)
        exec(functions(base / 'services/order_invoices.py'), order_ns)
        self.orders.is_order_invoice = order_ns['is_order_invoice']
        self.ns = dict(frappe=self.frappe, Decimal=Decimal, ROUND_HALF_UP=ROUND_HALF_UP, time=time,
                       hashlib=hashlib, hmac=hmac, json=json, urlencode=urlencode, urlparse=urlparse,
                       cint=lambda v: int(v or 0), SETTINGS='QAS Stripe Settings', PAYMENT='QAS Stripe Payment',
                       TEST_USER='rogerxu130@gmail.com', TRIAL_SOURCES={'Trial Inquiry', 'Replacement Trial Inquiry'},
                       get_invoice_payable_amount=lambda doc: doc.outstanding_amount)
        exec(functions(base / 'services/stripe_trial_payments.py'), self.ns)
        self.ns['configured'] = lambda config: True
        self.ns['settings'] = lambda: self.config
        self.stripe = ModuleType('qas_custom.services.stripe_trial_payments')
        self.stripe.payment_url = Mock(return_value='https://portal.example/order-payment#token=secret')
        invoice_settings = ModuleType('qas_custom.modules.billing.invoice_settings')
        invoice_settings.get_invoice_payment_context = lambda doc: {field: doc.get('qas_' + field) for field in ('bank_account_name', 'bank_bsb', 'bank_account_number', 'bank_reference_note')}
        self.modules = patch.dict('sys.modules', {'qas_custom.modules.billing.invoice_settings': invoice_settings, ** {'frappe': self.frappe, 'qas_custom.services.order_invoices': self.orders,
                                                   'qas_custom.services.stripe_trial_payments': self.stripe}})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.order_ns = order_ns
        self.order_ns['financials'] = self.orders.financials
        self.attempt = Doc(name='A1', invoice='OI-1', mode='Live', amount_cents=12900, currency='aud', checkout_session='cs_1', status='Open')
        self.attempt.save = Mock()
        self.session = dict(id='cs_1', metadata={'qas_attempt': 'A1', 'qas_invoice': 'OI-1'}, client_reference_id='A1',
                            amount_total=12900, currency='aud', livemode=True, payment_status='paid', payment_intent='pi_1')

    def test_order_parent_does_not_require_trial_pilot_identity(self):
        self.assertIs(self.ns['payment_parent'](self.invoice, self.config), self.parent)
        self.assertTrue(self.ns['eligible'](self.invoice, self.config))

    def test_live_trials_remain_open_to_ordinary_parents(self):
        self.invoice.update(qas_invoice_type='Trial', source_doctype='Inquiry', source_type='Trial Inquiry')
        self.assertTrue(self.ns['eligible'](self.invoice, self.config))

    def test_live_rejects_invoices_marked_as_stripe_test(self):
        self.invoice.qas_stripe_test = 1
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))

    def test_trial_test_gate_is_preserved(self):
        self.config.mode = 'Test'
        self.invoice.update(qas_invoice_type='Trial', source_doctype='Inquiry', source_type='Trial Inquiry')
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))
        self.parent.linked_user = 'rogerxu130@gmail.com'
        self.assertTrue(self.ns['eligible'](self.invoice, self.config))

    def test_order_customer_and_parent_must_match(self):
        for field in ('customer', 'parent'):
            with self.subTest(field=field):
                old = self.order[field]
                self.order[field] = 'different'
                self.assertFalse(self.ns['eligible'](self.invoice, self.config))
                self.order[field] = old

    def test_unlinked_or_inactive_parent_cannot_pay(self):
        self.parent.customer = 'another customer'
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))
        self.parent.customer = 'C1'
        self.parent.status = 'Inactive'
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))

    def test_draft_cancelled_return_currency_company_and_zero_rejected(self):
        for change in (dict(docstatus=0), dict(docstatus=2), dict(is_return=1), dict(currency='USD'), dict(company='Other'), dict(outstanding_amount=0)):
            with self.subTest(change=change):
                original = self.invoice.copy()
                self.invoice.update(change)
                self.assertFalse(self.ns['eligible'](self.invoice, self.config))
                self.invoice.clear(); self.invoice.update(original)

    def test_cancelled_or_pending_order_cannot_pay(self):
        self.order.status = 'Cancelled'
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))
        self.order.status = 'Ordered'; self.order.pending_revision = '{}'
        self.assertFalse(self.ns['eligible'](self.invoice, self.config))

    def test_credit_adjustments_cap_invoice_payment_to_order_balance(self):
        self.state['remaining_amount'] = 29
        self.assertEqual(self.ns['payable_amount'](self.invoice), 29)
        self.invoice.outstanding_amount = 9
        self.assertEqual(self.ns['payable_amount'](self.invoice), 9)

    def test_paid_or_draft_order_has_no_online_balance(self):
        for status in ('Paid', 'Draft', 'Cancelled'):
            self.state['invoice_status'] = status
            self.assertEqual(self.ns['payable_amount'](self.invoice), 0)

    def test_payment_options_use_invoice_bank_snapshot_and_reference(self):
        result = self.order_ns['payment_options'](self.order)
        self.assertEqual(result[0]['bank_bsb'], '123-456')
        self.assertEqual(result[0]['bank_reference'], 'OI-1')
        self.assertEqual(result[0]['amount'], 129)
        self.assertIn('/order-payment', result[0]['online_payment_url'])

    def test_payment_options_exclude_drafts_returns_cancelled_and_zero(self):
        self.state['invoices'] = [Doc(name='draft', docstatus=0, outstanding_amount=129), Doc(name='cancelled', docstatus=2, outstanding_amount=129), Doc(name='credit', docstatus=1, is_return=1, outstanding_amount=129), Doc(name='paid', docstatus=1, outstanding_amount=0)]
        self.assertEqual(self.order_ns['payment_options'](self.order), [])

    def test_supplemental_invoice_options_do_not_overstate_aggregate_debt(self):
        self.state['invoices'].append(Doc(name='OI-2', docstatus=1, outstanding_amount=50))
        self.state['remaining_amount'] = 149
        result = self.order_ns['payment_options'](self.order)
        self.assertEqual([row['amount'] for row in result], [129, 20])
        self.invoice.name = 'OI-2'
        self.invoice.outstanding_amount = 50
        self.assertEqual(self.ns['payable_amount'](self.invoice), 20)

    def test_no_options_for_pending_revision(self):
        self.order.pending_revision = '{}'
        self.assertEqual(self.order_ns['payment_options'](self.order), [])

    def test_stale_amount_after_bank_transfer_requires_review(self):
        self.state['remaining_amount'] = 100
        self.ns['settle'](self.session, self.config, 'evt_1')
        self.assertEqual(self.attempt.status, 'Needs Review')
        self.assertFalse(self.attempt.payment_entry)

    def test_order_cancelled_during_checkout_requires_review(self):
        self.order.status = 'Cancelled'
        self.ns['settle'](self.session, self.config, 'evt_1')
        self.assertEqual(self.attempt.status, 'Needs Review')

    def test_duplicate_webhook_does_not_post_again(self):
        self.attempt.status = 'Paid'
        self.ns['create_payment_entry'] = Mock()
        self.ns['settle'](self.session, self.config, 'evt_1')
        self.ns['create_payment_entry'].assert_not_called()

    def test_unpaid_session_does_not_post(self):
        self.session['payment_status'] = 'unpaid'
        self.ns['settle'](self.session, self.config, 'evt_1')
        self.assertEqual(self.attempt.status, 'Open')

    def test_forged_amount_or_invoice_is_rejected(self):
        for field, value in [('amount_total', 1), ('client_reference_id', 'other'), ('currency', 'usd')]:
            with self.subTest(field=field):
                changed = dict(self.session, **{field: value})
                with self.assertRaises(ValueError): self.ns['settle'](changed, self.config, 'evt_1')

    def test_test_payment_never_posts_real_money(self):
        self.config.mode = self.attempt.mode = 'Test'; self.session['livemode'] = False
        self.parent.linked_user = 'rogerxu130@gmail.com'
        self.ns['create_payment_entry'] = Mock()
        self.ns['settle'](self.session, self.config, 'evt_1')
        self.assertEqual(self.attempt.status, 'Test Paid')
        self.ns['create_payment_entry'].assert_not_called()

    def test_live_payment_posts_once_and_queues_receipt(self):
        service = ModuleType('qas_custom.services.school_admin')
        service._enqueue_paid_receipt = Mock(return_value={'queued': 1})
        self.ns['payment_mutations_enabled'] = lambda: True
        self.ns['create_payment_entry'] = Mock(return_value=Doc(name='PE1'))
        commands = ModuleType('qas_custom.modules.notifications.commands')
        commands.notify_stripe_invoice_paid = Mock()
        with patch.dict('sys.modules', {'qas_custom.services.school_admin': service, 'qas_custom.modules.notifications.commands': commands}):
            self.ns['settle'](self.session, self.config, 'evt_1')
            self.ns['settle'](self.session, self.config, 'evt_1')
        self.ns['create_payment_entry'].assert_called_once()
        service._enqueue_paid_receipt.assert_called_once()
        self.assertEqual(self.attempt.payment_entry, 'PE1')
        self.assertEqual(self.attempt.status, 'Paid')
        self.assertEqual(self.attempt.receipt_queued, 1)

    def test_authorization_rechecks_token_owner(self):
        self.ns['decode_token'] = lambda token, config: ('OI-1', 'P2')
        with self.assertRaises(ValueError): self.ns['authorize']('token')

    def test_support_view_cannot_create_payment_links(self):
        self.frappe.local.request = SimpleNamespace(headers={'X-QAS-Support-View-Token': 'support'})
        self.assertEqual(self.ns['payment_url'](self.invoice), '')

    def test_unsigned_or_modified_webhook_is_rejected(self):
        body = b'{"type":"checkout.session.completed"}'
        now = int(time.time())
        signature = hmac.new(b'secret', str(now).encode() + b'.' + body, hashlib.sha256).hexdigest()
        self.assertEqual(self.ns['verify_event'](body, f't={now},v1={signature}', 'secret')['type'], 'checkout.session.completed')
        with self.assertRaises(ValueError): self.ns['verify_event'](body + b' ', f't={now},v1={signature}', 'secret')
        with self.assertRaises(ValueError): self.ns['verify_event'](body, f't={now},v1={signature}', 'secret', now=now + 301)

    def checkout(self, during_create=None):
        presentation = ModuleType('qas_custom.modules.billing.presentation')
        presentation.parent_portal_invoice_link = lambda name: 'https://portal.example/invoices'
        self.ns['authorize'] = lambda token: (self.invoice, self.config)
        self.ns['validate_accounts'] = Mock()
        original_get_doc = self.frappe.get_doc

        def get_doc(kind, name=None):
            if isinstance(kind, dict):
                self.attempt.update(kind)
                self.attempt.insert = lambda **kwargs: self.attempt
                return self.attempt
            return original_get_doc(kind, name)

        self.frappe.get_doc = get_doc
        self.attempt.reload = Mock()

        def gateway(config, method, path, data=None, idempotency_key=None):
            if path == 'checkout/sessions':
                if during_create: during_create()
                return dict(id='cs_1', url='https://checkout.stripe.com/c/pay')
            return {}

        self.ns['stripe_request'] = Mock(side_effect=gateway)
        with patch.dict('sys.modules', {'qas_custom.modules.billing.presentation': presentation}):
            return self.ns['start_checkout']('token')

    def test_checkout_uses_current_order_debt_and_order_return_route(self):
        self.state['remaining_amount'] = 29
        result = self.checkout()
        self.assertEqual(result['url'], 'https://checkout.stripe.com/c/pay')
        request = self.ns['stripe_request'].call_args.args[3]
        self.assertEqual(request['line_items[0][price_data][unit_amount]'], 2900)
        self.assertIn('QAS order invoice', request['line_items[0][price_data][product_data][name]'])
        self.assertIn('/order-payment?attempt=', request['success_url'])

    def test_revision_during_gateway_request_expires_checkout(self):
        with self.assertRaisesRegex(ValueError, 'Invoice changed'):
            self.checkout(lambda: self.order.update(pending_revision='{}'))
        self.assertEqual(self.attempt.status, 'Expired')
        self.assertEqual(self.ns['stripe_request'].call_args.args[2], 'checkout/sessions/cs_1/expire')


if __name__ == '__main__':
    unittest.main()
