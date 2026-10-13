from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

from qas_custom.services import paid_invoice_payments as service


class TestPaidInvoicePayments(TestCase):
    def setUp(self):
        self.data = {}
        self.reader = patch.object(service, '_linked', side_effect=self.linked)
        self.reader.start()
        self.addCleanup(patch.stopall)

    def linked(self, doctype, field, names, fields, filters=None):
        return [deepcopy(row) for row in self.data.get(doctype, []) if row.get(field) in names]

    def invoice(self, name='I1', total=540, **kwargs):
        self.data.setdefault('Payment Ledger Entry', []).append({
            'against_voucher_no': name, 'voucher_type': 'Sales Invoice', 'voucher_no': name,
            'amount_in_account_currency': total, 'account_currency': 'AUD', 'delinked': 0})
        return {'name': name, 'payment_report_total': total, 'currency': 'AUD', **kwargs}

    def payment(self, value, invoice='I1', voucher='P1', **kwargs):
        self.data.setdefault('Payment Entry', []).append({'name': voucher, 'docstatus': 1,
            'payment_type': 'Receive' if value > 0 else 'Pay', **kwargs})
        self.ledger(value, invoice, voucher, 'Payment Entry')

    def ledger(self, value, invoice, voucher, kind, **kwargs):
        self.data.setdefault('Payment Ledger Entry', []).append({
            'against_voucher_no': invoice, 'voucher_type': kind, 'voucher_no': voucher,
            'amount_in_account_currency': -value, 'account_currency': 'AUD', 'delinked': 0, **kwargs})

    def credit(self, value, invoice='I1', voucher='J1', **kwargs):
        self.data.setdefault('Journal Entry', []).append({'name': voucher, 'docstatus': 1,
            'qas_store_credit_invoice': invoice, 'qas_store_credit_amount': value, **kwargs})
        self.data.setdefault('QAS Store Credit Ledger', []).append({'invoice': invoice,
            'journal_entry': voucher, 'transaction_type': 'Invoice Application', 'debit_amount': value})
        self.ledger(value, invoice, voucher, 'Journal Entry')

    def test_direct_credit_mixed_and_review_reconcile_to_all_paid_invoices(self):
        invoices = [self.invoice('cash', 540), self.invoice('credit', 270),
                    self.invoice('mixed', 540), self.invoice('unknown', 100)]
        self.payment(540, 'cash', 'P1')
        self.credit(270, 'credit', 'J1')
        self.payment(200, 'mixed', 'P2')
        self.credit(340, 'mixed', 'J2')
        result = service.paid_invoice_payment_breakdown(invoices)
        self.assertEqual([g['count'] for g in result['groups']], [1, 1, 1, 1])
        self.assertEqual(sum(g['invoice_total'] for g in result['groups']), 1450)
        self.assertEqual(result['cash_amount'], 740)
        self.assertEqual(result['credit_amount'], 610)
        self.assertEqual(result['review_count'], 1)
        self.assertFalse(result['complete'])

    def test_duplicate_credit_snapshot_and_split_ledger_do_not_double_count(self):
        invoice = self.invoice()
        self.credit(540)
        self.data['QAS Store Credit Ledger'] *= 2
        row = self.data['Payment Ledger Entry'][-1]
        row['amount_in_account_currency'] = -200
        self.ledger(340, 'I1', 'J1', 'Journal Entry')
        self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['credit_amount'], 540)

    def test_credit_can_be_identified_by_ledger_without_header_marker(self):
        invoice = self.invoice()
        self.credit(540, qas_store_credit_invoice=None, qas_store_credit_amount=0)
        self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['credit_amount'], 540)

    def test_delinked_allocations_are_ignored_and_signed_reversals_are_net(self):
        invoice = self.invoice()
        self.payment(600)
        self.payment(-60, voucher='REFUND')
        self.ledger(999, 'I1', 'OLD', 'Payment Entry', delinked=1)
        self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['cash_amount'], 540)

    def test_pos_change_and_allocated_payment(self):
        invoice = self.invoice(is_pos=1, paid_amount=300, change_amount=20)
        self.payment(260)
        result = service.paid_invoice_payment_breakdown([invoice])
        self.assertTrue(result['complete'])
        self.assertEqual(result['cash_amount'], 540)

    def test_missing_payment_credit_snapshot_and_unknown_journal_need_review(self):
        for case in ('missing', 'snapshot', 'journal'):
            with self.subTest(case=case):
                self.data = {}
                invoice = self.invoice()
                if case == 'snapshot':
                    self.data['QAS Store Credit Ledger'] = [{'invoice': 'I1', 'transaction_type': 'Invoice Application', 'debit_amount': 540}]
                if case == 'journal':
                    self.data['Journal Entry'] = [{'name': 'ADJUST', 'docstatus': 1}]
                    self.ledger(540, 'I1', 'ADJUST', 'Journal Entry')
                result = service.paid_invoice_payment_breakdown([invoice])
                self.assertEqual(result['review_count'], 1)
                self.assertEqual(result['cash_amount'] + result['credit_amount'], 0)

    def test_deductions_cancelled_currency_zero_and_returns_need_review(self):
        for overrides, payment in [({}, {'difference_amount': 10}), ({}, {'docstatus': 2}),
                                  ({'currency': 'USD'}, {}), ({'is_return': 1}, {}),
                                  ({'total': 0}, {})]:
            with self.subTest(overrides=overrides, payment=payment):
                self.data = {}
                invoice = self.invoice(**overrides)
                self.payment(invoice['payment_report_total'], **payment)
                self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['review_count'], 1)

    def test_one_cent_difference_is_not_silently_assigned_to_cash(self):
        invoice = self.invoice(total='100.01')
        self.payment(100)
        self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['review_count'], 1)

    def test_empty_summary_needs_no_database_queries(self):
        result = service.paid_invoice_payment_breakdown([])
        self.assertEqual(result['cash_amount'], 0)
        self.assertEqual(result['credit_amount'], 0)
        self.assertTrue(result['complete'])

    def test_reader_preserves_filters_and_batches_invoice_names(self):
        self.reader.stop()
        with patch.object(service.frappe, 'db', new=SimpleNamespace(exists=lambda *_args: True)), \
             patch.object(service.frappe, 'get_meta', return_value=SimpleNamespace(has_field=lambda f: f != 'unknown')), \
             patch.object(service.frappe, 'get_all', return_value=[]) as read:
            service._linked('Payment Ledger Entry', 'against_voucher_no', [f'I{i}' for i in range(501)],
                            ['name', 'account_currency', 'unknown'], {'delinked': 0})
            self.assertEqual(read.call_count, 2)
            self.assertEqual([len(c.kwargs['filters']['against_voucher_no'][1]) for c in read.call_args_list], [500, 1])
            self.assertEqual(read.call_args.kwargs['filters']['delinked'], 0)
            self.assertEqual(read.call_args.kwargs['fields'], ['name', 'account_currency'])

    def test_legacy_credit_correction_and_conflicting_journal_link_need_review(self):
        for case in ('correction', 'conflict'):
            with self.subTest(case=case):
                self.data = {}
                invoice = self.invoice()
                self.credit(540)
                if case == 'correction':
                    self.data['QAS Store Credit Ledger'].append({'invoice': 'I1', 'transaction_type': 'Correction', 'credit_amount': 20})
                else:
                    self.data['Journal Entry'][0]['qas_store_credit_invoice'] = 'OTHER'
                self.assertEqual(service.paid_invoice_payment_breakdown([invoice])['review_count'], 1)
