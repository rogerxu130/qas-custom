from copy import deepcopy
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe
from qas_custom.api import course_revenue as api
from qas_custom.services.course_revenue_calculation import allocate, invoice_allocations


class TestAllocations(TestCase):
    def line(self, name='L1', fee=540):
        return {'name': name, 'net_amount': fee}

    def test_unpaid_enrollment_contributes_zero(self):
        row = invoice_allocations(540, [self.line()], 0, 0)[0]
        self.assertEqual(row['eligible_revenue'], 0)

    def test_full_partial_and_mid_term_fees(self):
        for fee, paid, expected in [(540, 540, 540), (540, 200, 200), (270, 270, 270)]:
            self.assertEqual(invoice_allocations(fee, [self.line(fee=fee)], paid, 0)[0]['eligible_revenue'], expected)

    def test_credit_is_consumption_not_account_balance(self):
        row = invoice_allocations(540, [self.line()], 200, 340)[0]
        self.assertEqual(row['cash_received'], 200)
        self.assertEqual(row['credit_used'], 340)
        self.assertEqual(row['eligible_revenue'], 540)

    def test_mixed_courses_partial_payment(self):
        rows = invoice_allocations(1000, [self.line('A', 600), self.line('B', 400)], 200, 300)
        self.assertEqual([r['eligible_revenue'] for r in rows], [300, 200])
        self.assertEqual([r['cash_received'] for r in rows], [120, 80])
        self.assertEqual([r['credit_used'] for r in rows], [180, 120])

    def test_unknown_charge_is_not_redistributed(self):
        rows = invoice_allocations(1000, [self.line('A', 600), self.line('unknown', 400)], 500, 0)
        self.assertEqual(rows[0]['eligible_revenue'], 300)

    def test_exact_rounding_and_invoice_wide_adjustment(self):
        self.assertEqual(allocate('.01', [1, 1, 1]), [Decimal('.01'), Decimal(0), Decimal(0)])
        for cents in range(1, 150):
            parts = allocate(Decimal(cents) / 100, [3, 7, 9])
            self.assertEqual(sum(parts), Decimal(cents) / 100)
        rows = invoice_allocations(90, [self.line('A', 60), self.line('B', 40)], 90, 0)
        self.assertEqual([r['billed_amount'] for r in rows], [54, 36])

    def test_overpayment_is_capped(self):
        self.assertEqual(invoice_allocations(540, [self.line()], 1000, 0)[0]['eligible_revenue'], 540)

    def test_return_and_its_cash_refund_deduct_once(self):
        row = invoice_allocations(540, [self.line()], 540, 0, 100, {'L1': 100})[0]
        self.assertEqual(row['eligible_revenue'], 440)
        self.assertEqual(row['reductions'], 100)

    def test_return_reducing_unpaid_fees_is_not_cash_refund(self):
        row = invoice_allocations(540, [self.line()], 200, 0, 0, {'L1': 100})[0]
        self.assertEqual(row['eligible_revenue'], 200)
        row = invoice_allocations(540, [self.line()], 200, 0, 100, {'L1': 100})[0]
        self.assertEqual(row['eligible_revenue'], 100)

    def test_return_into_credit_reduces_original_course(self):
        row = invoice_allocations(540, [self.line()], 0, 540, 0, {'L1': 100})[0]
        self.assertEqual(row['eligible_revenue'], 440)

    def test_negative_charge_requires_review(self):
        with self.assertRaises(ValueError):
            invoice_allocations(100, [self.line('A', 120), self.line('B', -20)], 100, 0)

    def test_split_sources_never_create_a_rounding_refund(self):
        for total in range(1, 80):
            billed = Decimal(total) / 100
            for cash in range(total + 1):
                rows = invoice_allocations(billed, [self.line('A', 1), self.line('B', 1), self.line('C', 1)], Decimal(cash)/100, billed-Decimal(cash)/100)
                self.assertEqual(sum(Decimal(str(r['reductions'])) for r in rows), 0)
                self.assertEqual(sum(Decimal(str(r['eligible_revenue'])) for r in rows), billed)
                self.assertEqual(sum(Decimal(str(r['cash_received'])) for r in rows), Decimal(cash)/100)

    def test_targeted_return_refund_is_not_spread_to_other_course(self):
        rows = invoice_allocations(1000, [self.line('A', 600), self.line('B', 400)], 1000, 0, 100, {'A': 100})
        self.assertEqual([r['eligible_revenue'] for r in rows], [500, 400])

    def test_refund_of_overpayment_does_not_reduce_course_income(self):
        row = invoice_allocations(540, [self.line()], 600, 0, 60)[0]
        self.assertEqual(row['eligible_revenue'], 540)
        self.assertEqual(row['reductions'], 0)


class TestReportRecords(TestCase):
    def setUp(self):
        self.data = {
            'Sales Invoice': [{'name': 'I1', 'docstatus': 1, 'term': 'T3', 'currency': 'AUD', 'grand_total': 540, 'outstanding_amount': 540}],
            'Sales Invoice Item': [{'name': 'L1', 'parent': 'I1', 'parenttype': 'Sales Invoice', 'term': 'T3', 'course': 'Art', 'student': 'S1', 'net_amount': 540, 'qas_line_type': 'Course Fee'}],
            'Payment Ledger Entry': [{'name': 'PLE1', 'against_voucher_no': 'I1', 'against_voucher_type': 'Sales Invoice', 'voucher_type': 'Sales Invoice', 'voucher_no': 'I1', 'amount_in_account_currency': 540, 'account_currency': 'AUD', 'account_type': 'Receivable', 'party_type': 'Customer', 'delinked': 0}],
        }
        self.reader = patch.object(api, '_rows', side_effect=self.rows).start()
        self.addCleanup(patch.stopall)

    def rows(self, doctype, fields, filters=None):
        rows = deepcopy(self.data.get(doctype, []))
        def match(row):
            for key, expected in (filters or {}).items():
                if isinstance(expected, list) and expected[0] == 'in':
                    if row.get(key) not in expected[1]:
                        return False
                elif row.get(key) != expected:
                    return False
            return True
        return [r for r in rows if match(r)]

    def payment(self, paid, kind='Payment Entry', voucher='P1', invoice='I1', status=1, **extra):
        self.data.setdefault(kind, []).append({'name': voucher, 'docstatus': status, **extra})
        self.data['Payment Ledger Entry'].append({
            **self.data['Payment Ledger Entry'][0], 'name': 'PLE-' + voucher,
            'against_voucher_no': invoice, 'voucher_type': kind, 'voucher_no': voucher,
            'amount_in_account_currency': -paid,
        })
        header = next(h for h in self.data['Sales Invoice'] if h['name'] == invoice)
        header['outstanding_amount'] -= paid

    def result(self):
        return api._build_report('T3', 'Art')

    def test_enrolled_unpaid_is_zero_without_reading_attendance(self):
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 0)
        self.assertEqual(r['items'][0]['eligible_revenue'], 0)
        self.assertNotIn('Class Attendance Entry', [call.args[0] for call in self.reader.call_args_list])

    def test_partly_paid_invoice_counts_paid_amount(self):
        self.payment(200)
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 200)

    def test_credit_journal_and_ledger_are_counted_once(self):
        self.payment(200)
        self.payment(340, 'Journal Entry', 'J1', qas_store_credit_invoice='I1', qas_store_credit_amount=340)
        self.data['QAS Store Credit Ledger'] = [{'name': 'C1', 'invoice': 'I1', 'journal_entry': 'J1', 'transaction_type': 'Invoice Application', 'debit_amount': 340}]
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 540)
        self.assertEqual(r['summary']['credit_used'], 340)

    def test_unallocated_customer_credit_is_not_counted(self):
        self.data['QAS Store Credit Ledger'] = [{'name': 'C1', 'transaction_type': 'Top-up', 'credit_amount': 1500}]
        self.assertEqual(self.result()['summary']['eligible_revenue'], 0)

    def test_writeoff_is_not_cash(self):
        self.payment(540, 'Journal Entry', 'WRITE-OFF')
        r = self.result()
        self.assertFalse(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 0)
        self.assertEqual(r['diagnostics'][0]['code'], 'unclassified_settlement')

    def test_cancelled_invoice_is_excluded(self):
        self.data['Sales Invoice'][0]['docstatus'] = 2
        self.assertEqual(self.result()['items'], [])

    def test_delinked_cancelled_payment_is_excluded(self):
        self.payment(540)
        self.data['Payment Ledger Entry'][-1]['delinked'] = 1
        self.data['Sales Invoice'][0]['outstanding_amount'] = 540
        self.assertEqual(self.result()['summary']['eligible_revenue'], 0)

    def test_mixed_term_and_course_allocation(self):
        self.data['Sales Invoice'][0].update(grand_total=1000, outstanding_amount=1000)
        self.data['Payment Ledger Entry'][0]['amount_in_account_currency'] = 1000
        self.data['Sales Invoice Item'][0]['net_amount'] = 600
        self.data['Sales Invoice Item'].append({**self.data['Sales Invoice Item'][0], 'name': 'L2', 'term': 'T4', 'course': 'Other', 'net_amount': 400})
        self.payment(500)
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 300)
        self.assertEqual(len(r['items']), 1)

    def test_trial_without_invoice_term_resolves_source_inquiry(self):
        self.data['Sales Invoice'][0].update(term=None, source_doctype='Inquiry', source_document='Q1')
        self.data['Sales Invoice Item'][0].update(term=None, course_session='A display label')
        self.data['Inquiry'] = [{'name': 'Q1', 'course_session': 'SESSION1', 'student': 'S1'}]
        self.data['Course Sessions'] = [{'name': 'SESSION1', 'weekly_timeslot': 'WT1'}]
        self.data['Weekly Timeslot'] = [{'name': 'WT1', 'term': 'T3', 'course': 'Art'}]
        self.payment(540)
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 540)
        self.assertEqual(r['items'][0]['lesson_type'], 'Trial')

    def test_inactive_enrollment_only_resolves_attribution(self):
        self.data['Sales Invoice'][0]['term'] = None
        self.data['Sales Invoice Item'][0].update(term=None, course=None, enrollment='E1')
        self.data['Enrollment'] = [{'name': 'E1', 'status': 'Inactive', 'term': 'T3', 'course': 'Art'}]
        self.payment(540)
        self.assertEqual(self.result()['summary']['eligible_revenue'], 540)

    def test_pos_payment_counts_once(self):
        self.data['Sales Invoice'][0].update(is_pos=1, paid_amount=550, change_amount=10, outstanding_amount=0)
        self.data['Payment Ledger Entry'][0]['amount_in_account_currency'] = 0
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 540)

    def test_missing_legacy_term_is_reported_not_guessed(self):
        self.data['Sales Invoice'][0]['term'] = None
        self.data['Sales Invoice Item'][0]['term'] = None
        r = self.result()
        self.assertFalse(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 0)

    def test_unsupported_currency_does_not_mix_into_aud(self):
        self.data['Sales Invoice'][0]['currency'] = 'USD'
        self.assertFalse(self.result()['complete'])

    def add_return(self, fee=100):
        self.data['Sales Invoice'].append({'name': 'R1', 'docstatus': 1, 'is_return': 1, 'return_against': 'I1', 'currency': 'AUD', 'grand_total': -fee, 'outstanding_amount': -fee})
        self.data['Sales Invoice Item'].append({**self.data['Sales Invoice Item'][0], 'name': 'RL1', 'parent': 'R1', 'net_amount': -fee, 'sales_invoice_item': 'L1'})
        self.data['Payment Ledger Entry'].append({**self.data['Payment Ledger Entry'][0], 'name': 'PLE-R1', 'voucher_no': 'R1', 'amount_in_account_currency': -fee})
        self.data['Sales Invoice'][0]['outstanding_amount'] -= fee

    def test_return_and_refund_against_original_count_once(self):
        self.payment(540)
        self.add_return()
        self.payment(-100, voucher='REFUND')
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 440)

    def test_partial_payment_refunded_against_credit_note(self):
        self.payment(200)
        self.add_return()
        self.payment(-100, voucher='REFUND', invoice='R1')
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 100)

    def test_unpaid_reduction_is_not_a_payment(self):
        self.add_return()
        r = self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 0)

    def test_permissions_precede_all_reads(self):
        with patch.object(api, '_require_school_admin', side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                api.report.__wrapped__('T3', 'Art')
            with self.assertRaises(frappe.PermissionError):
                api.options.__wrapped__()
        self.reader.assert_not_called()

    def test_missing_payment_ledger_cannot_fake_paid_status(self):
        self.data['Sales Invoice'][0].update(status='Paid', outstanding_amount=0)
        r = self.result()
        self.assertFalse(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 0)

    def test_payment_deduction_is_not_counted_as_cash(self):
        self.payment(540)
        self.data['Payment Entry Deduction'] = [{'name':'D1', 'parent':'P1', 'amount':40}]
        r = self.result()
        self.assertFalse(r['complete'])
        self.assertEqual(r['summary']['cash_received'], 0)

    def test_full_paid_multi_line_return_is_attributed_once(self):
        self.data['Sales Invoice'][0].update(grand_total=1000, outstanding_amount=1000)
        self.data['Payment Ledger Entry'][0]['amount_in_account_currency'] = 1000
        self.data['Sales Invoice Item'][0]['net_amount'] = 600
        self.data['Sales Invoice Item'].append({**self.data['Sales Invoice Item'][0], 'name':'L2', 'course':'Music', 'net_amount':400})
        self.payment(1000)
        self.add_return(100)
        self.payment(-100, voucher='REFUND')
        r=self.result()
        self.assertTrue(r['complete'])
        self.assertEqual(r['summary']['eligible_revenue'], 500)

    def test_partial_multi_line_return_requires_review(self):
        self.data['Sales Invoice Item'][0]['net_amount'] = 300
        self.data['Sales Invoice Item'].append({**self.data['Sales Invoice Item'][0], 'name':'L2', 'course':'Music', 'net_amount':240})
        self.payment(200)
        self.add_return(100)
        r=self.result()
        self.assertFalse(r['complete'])
        self.assertIn('mixed_course_return', [d['code'] for d in r['diagnostics']])

    def test_support_view_denied_before_read(self):
        with patch.object(api, '_require_school_admin'), patch.object(api, 'get_support_view_token', return_value='support'), patch.object(api.frappe, 'throw', side_effect=frappe.PermissionError), patch.object(api, '_', side_effect=lambda text:text):
            with self.assertRaises(frappe.PermissionError):
                api.report.__wrapped__('T3','Art')
        self.reader.assert_not_called()
