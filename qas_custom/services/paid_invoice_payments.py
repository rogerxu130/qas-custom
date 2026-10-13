"""Verify payment sources for whole Paid invoices, without changing accounting data."""
from collections import defaultdict

import frappe

from qas_custom.services.course_revenue_calculation import amount


def _linked(doctype, field, names, fields, filters=None):
    names = sorted(set(names))
    if not names or not frappe.db.exists('DocType', doctype):
        return []
    meta = frappe.get_meta(doctype)
    fields = [f for f in fields if f in ('name', 'parent', 'docstatus') or meta.has_field(f)]
    rows = []
    for start in range(0, len(names), 500):
        rows.extend(frappe.get_all(doctype, fields=fields,
            filters={**(filters or {}), field: ['in', names[start:start + 500]]}, limit_page_length=0))
    return rows


def paid_invoice_payment_breakdown(invoices):
    """Unknown adjustments stay outside verified totals; category face values reconcile."""
    groups = {key: {'key': key, 'count': 0, 'invoice_total': amount(0),
                    'cash_amount': amount(0), 'credit_amount': amount(0)}
              for key in ('direct', 'store_credit', 'mixed', 'review')}
    headers = {row['name']: row for row in invoices}
    diagnostics = []
    issues = defaultdict(set)
    settlements = {name: {'cash': amount(0), 'credit': amount(0)} for name in headers}
    ledger = _linked('Payment Ledger Entry', 'against_voucher_no', headers,
        ['name', 'against_voucher_no', 'voucher_type', 'voucher_no', 'amount_in_account_currency',
         'account_currency', 'delinked'],
        {'against_voucher_type': 'Sales Invoice', 'account_type': 'Receivable',
         'party_type': 'Customer', 'delinked': 0})
    payments = {r['name']: r for r in _linked('Payment Entry', 'name',
        [r['voucher_no'] for r in ledger if r['voucher_type'] == 'Payment Entry'],
        ['name', 'docstatus', 'payment_type', 'difference_amount'])}
    deductions = {r['parent'] for r in _linked('Payment Entry Deduction', 'parent', payments,
        ['name', 'parent', 'amount']) if amount(r.get('amount'))}
    journals = {r['name']: r for r in _linked('Journal Entry', 'name',
        [r['voucher_no'] for r in ledger if r['voucher_type'] == 'Journal Entry'],
        ['name', 'docstatus', 'qas_store_credit_invoice', 'qas_store_credit_amount'])}
    credit_rows = _linked('QAS Store Credit Ledger', 'invoice', headers,
        ['name', 'invoice', 'journal_entry', 'transaction_type', 'debit_amount', 'credit_amount'])
    credit_links = defaultdict(set)
    for row in credit_rows:
        if row.get('transaction_type') == 'Invoice Application' and row.get('journal_entry'):
            credit_links[row['journal_entry']].add(row['invoice'])
    for name, journal in journals.items():
        if journal.get('qas_store_credit_invoice') and amount(journal.get('qas_store_credit_amount')) > 0:
            credit_links[name].add(journal['qas_store_credit_invoice'])

    grouped = defaultdict(lambda: amount(0))
    for row in ledger:
        invoice = row['against_voucher_no']
        if invoice not in headers or row.get('delinked'):
            continue
        if row.get('account_currency') != 'AUD':
            issues[invoice].add('unsupported_currency')
            continue
        grouped[(invoice, row['voucher_type'], row['voucher_no'])] -= amount(row.get('amount_in_account_currency'))
    for (invoice, kind, voucher), value in grouped.items():
        if not value:
            continue
        if kind == 'Sales Invoice' and voucher == invoice:
            continue  # The invoice's receivable posting is not a payment.
        if kind == 'Payment Entry':
            payment = payments.get(voucher, {})
            if payment.get('docstatus') != 1:
                issues[invoice].add('unsubmitted_payment')
            elif voucher in deductions or amount(payment.get('difference_amount')):
                issues[invoice].add('payment_deductions')
            elif payment.get('payment_type') not in ('Receive', 'Pay'):
                issues[invoice].add('unclassified_settlement')
            else:
                settlements[invoice]['cash'] += value
        elif kind == 'Journal Entry':
            journal = journals.get(voucher, {})
            if journal.get('docstatus') != 1:
                issues[invoice].add('unsubmitted_payment')
            elif credit_links.get(voucher) != {invoice}:
                issues[invoice].add('unclassified_settlement')
            else:
                settlements[invoice]['credit'] += value
        else:
            issues[invoice].add('unclassified_settlement')

    for row in credit_rows:
        invoice = row['invoice']
        if row.get('transaction_type') == 'Invoice Application' and amount(row.get('debit_amount')):
            # A consumed-credit snapshot alone is not evidence of a submitted settlement.
            linked = row.get('journal_entry')
            matching = any(j.get('docstatus') == 1 and credit_links.get(name) == {invoice}
                           and (invoice, 'Journal Entry', name) in grouped
                           for name, j in journals.items() if not linked or name == linked)
            if not matching:
                issues[invoice].add('unverified_credit')
        if row.get('transaction_type') in ('Correction', 'Withdrawal Credit') and amount(row.get('credit_amount')):
            issues[invoice].add('credit_adjustment')

    for name, header in headers.items():
        total = amount(header.get('payment_report_total'))
        cash, credit = settlements[name]['cash'], settlements[name]['credit']
        if header.get('currency') != 'AUD':
            issues[name].add('unsupported_currency')
        if header.get('is_return') or total <= 0:
            issues[name].add('nonpositive_invoice')
        if header.get('is_pos'):
            cash += amount(header.get('paid_amount')) - amount(header.get('change_amount'))
        if cash < 0 or credit < 0 or cash + credit != total:
            issues[name].add('settlement_mismatch')
        if issues[name]:
            key = 'review'
            diagnostics.append({'invoice': name, 'invoice_total': float(total), 'reasons': sorted(issues[name])})
        else:
            key = 'mixed' if cash and credit else ('store_credit' if credit else 'direct')
        group = groups[key]
        group['count'] += 1
        group['invoice_total'] += total
        if key != 'review':
            group['cash_amount'] += cash
            group['credit_amount'] += credit
    return {
        'groups': [{field: float(value) if field.endswith('_amount') or field == 'invoice_total' else value
                    for field, value in row.items()} for row in groups.values()],
        'cash_amount': float(sum((g['cash_amount'] for g in groups.values()), amount(0))),
        'credit_amount': float(sum((g['credit_amount'] for g in groups.values()), amount(0))),
        'complete': not diagnostics,
        'review_count': groups['review']['count'],
        'diagnostics': diagnostics,
    }
