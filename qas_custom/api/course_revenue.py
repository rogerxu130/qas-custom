"""Read-only School Admin course settlement reporting."""
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _

from qas_custom.services.course_revenue_calculation import allocate, amount, invoice_allocations
from qas_custom.services.school_admin_reporting import _require_school_admin, _validate_term
from qas_custom.services.support_view import get_support_view_token

BATCH_SIZE = 500


def _rows(doctype, fields, filters=None):
    meta = frappe.get_meta(doctype)
    safe_fields = [field for field in fields if field in {'name', 'docstatus', 'parent', 'idx'} or meta.has_field(field)]
    return frappe.get_all(doctype, filters=filters or {}, fields=safe_fields,
                          limit_page_length=0, order_by='name asc')


def _linked(doctype, field, values, fields, filters=None):
    values = sorted({v for v in values if v})
    result = []
    for start in range(0, len(values), BATCH_SIZE):
        result.extend(_rows(doctype, fields, {**(filters or {}), field: ['in', values[start:start + BATCH_SIZE]]}))
    return result


def _map(rows):
    return {row['name']: row for row in rows}


def _require_report_access():
    _require_school_admin()
    if get_support_view_token():
        frappe.throw(_('Course revenue is not available in Support View.'), frappe.PermissionError)


@frappe.whitelist()
def options():
    _require_report_access()
    return {'courses': _rows('Course', ['name', 'course_name', 'course_name_zh'])}


@frappe.whitelist()
def report(term=None, course=None):
    _require_report_access()
    _validate_term(term)
    if not course or not frappe.db.exists('Course', course):
        frappe.throw(_('Choose a valid Course.'))
    if not frappe.db.exists('DocType', 'Payment Ledger Entry'):
        frappe.throw(_('Payment Ledger Entry is required to verify actual settlements.'))
    result = _build_report(term, course)
    result.update(term=term, course=course, currency='AUD', calculated_at=datetime.now(ZoneInfo('Australia/Brisbane')).strftime('%Y-%m-%d %H:%M:%S'))
    return result


def _resolve_lines(headers, lines):
    """Resolve immutable invoice data first; source links fill only missing values.

    No enrollment status/attendance participates in eligibility or money calculations.
    Trial invoices may lack a Term and carry a display label, not a session ID, on
    their items. Their source Inquiry provides the actual session link.
    """
    enrollments = _map(_linked('Enrollment', 'name',
        [r.get('enrollment') for r in lines] + [h.get('enrollment') for h in headers.values()],
        ['name', 'term', 'course', 'student', 'weekly_timeslot']))
    inquiries = _map(_linked('Inquiry', 'name',
        [h.get('source_document') for h in headers.values() if h.get('source_doctype') == 'Inquiry'],
        ['name', 'course_session', 'student']))
    sessions = _map(_linked('Course Sessions', 'name',
        [r.get('course_session') for r in lines] + [q.get('course_session') for q in inquiries.values()],
        ['name', 'weekly_timeslot']))
    slots = _map(_linked('Weekly Timeslot', 'name',
        [s.get('weekly_timeslot') for s in sessions.values()] + [e.get('weekly_timeslot') for e in enrollments.values()],
        ['name', 'term', 'course']))
    resolved = []
    for raw in lines:
        line = dict(raw)
        header = headers[line['parent']]
        enrollment = enrollments.get(line.get('enrollment') or header.get('enrollment'), {})
        inquiry = inquiries.get(header.get('source_document'), {}) if header.get('source_doctype') == 'Inquiry' else {}
        session = sessions.get(line.get('course_session') or '', {}) or sessions.get(inquiry.get('course_session'), {})
        slot = slots.get(session.get('weekly_timeslot'), {}) or slots.get(enrollment.get('weekly_timeslot'), {})
        # A header Course must not attribute a miscellaneous fee to that Course.
        course_fee = line.get('qas_line_type') == 'Course Fee'
        line['course'] = line.get('course') or enrollment.get('course') or (slot.get('course') if course_fee or inquiry else None) or (header.get('course') if course_fee else None)
        line['term'] = line.get('term') or header.get('term') or enrollment.get('term') or slot.get('term')
        line['student'] = line.get('student') or enrollment.get('student') or inquiry.get('student') or header.get('primary_student') or header.get('student')
        line['lesson_type'] = 'Trial' if inquiry or header.get('source_type') in ('Trial Inquiry', 'Replacement Trial Inquiry') else 'Regular'
        resolved.append(line)
    students = _map(_linked('Student', 'name', [r.get('student') for r in resolved],
                            ['name', 'student_name', 'first_name', 'last_name']))
    for line in resolved:
        student = students.get(line.get('student'), {})
        line['student_name'] = line.get('student_display_name') or student.get('student_name') or ' '.join(filter(None, [student.get('first_name'), student.get('last_name')])) or line.get('student') or ''
    return resolved


def _build_report(term, course):
    # Headers and attribution are read in bulk. Settlements are fetched only for
    # candidate invoices after attribution, including trials without header Term.
    headers = _map(_rows('Sales Invoice', [
        'name', 'docstatus', 'is_return', 'return_against', 'term', 'course', 'enrollment',
        'student', 'primary_student', 'source_doctype', 'source_document', 'source_type',
        'currency', 'grand_total', 'rounded_total', 'disable_rounded_total', 'is_pos',
        'paid_amount', 'change_amount', 'posting_date', 'outstanding_amount', 'debit_to',
    ], {'docstatus': 1}))
    raw_lines = _linked('Sales Invoice Item', 'parent', headers, [
        'name', 'parent', 'idx', 'term', 'course', 'student', 'student_display_name', 'enrollment',
        'course_session', 'net_amount', 'amount', 'qas_line_type', 'sales_invoice_item',
    ], {'parenttype': 'Sales Invoice'})
    lines = _resolve_lines(headers, raw_lines)
    by_invoice = {}
    for line in lines:
        by_invoice.setdefault(line['parent'], []).append(line)
    diagnostics = []
    diagnostic_keys = set()

    def issue(invoice, code):
        key = (invoice, code)
        if key not in diagnostic_keys:
            diagnostics.append({'invoice': invoice, 'code': code})
            diagnostic_keys.add(key)

    selected = set()
    for line in lines:
        if headers[line['parent']].get('is_return'):
            continue
        if line.get('course') == course and line.get('term') == term:
            selected.add(line['parent'])
        elif ((line.get('course') == course and not line.get('term')) or
              (line.get('term') == term and not line.get('course') and line.get('qas_line_type') != 'Other')):
            issue(line['parent'], 'missing_attribution')
    returns = {name: h for name, h in headers.items() if h.get('is_return') and h.get('return_against') in selected}
    relevant = selected | set(returns)
    ledger = _linked('Payment Ledger Entry', 'against_voucher_no', relevant, [
        'name', 'against_voucher_no', 'voucher_type', 'voucher_no', 'amount_in_account_currency',
        'account_currency', 'account', 'delinked',
    ], {'against_voucher_type': 'Sales Invoice', 'account_type': 'Receivable', 'party_type': 'Customer', 'delinked': 0})
    payments = _map(_linked('Payment Entry', 'name', [r['voucher_no'] for r in ledger if r['voucher_type'] == 'Payment Entry'],
        ['name', 'docstatus', 'payment_type', 'difference_amount']))
    deductions = _linked('Payment Entry Deduction', 'parent', payments, ['name', 'parent', 'amount'])
    payment_with_deductions = {r['parent'] for r in deductions if amount(r.get('amount'))}
    journals = _map(_linked('Journal Entry', 'name', [r['voucher_no'] for r in ledger if r['voucher_type'] == 'Journal Entry'],
        ['name', 'docstatus', 'qas_store_credit_invoice', 'qas_store_credit_ledger', 'qas_store_credit_amount']))
    credit_ledger = _linked('QAS Store Credit Ledger', 'invoice', relevant,
        ['name', 'invoice', 'journal_entry', 'transaction_type', 'debit_amount', 'credit_amount'])
    credit_journals = {r['journal_entry']: r['invoice'] for r in credit_ledger
                       if r.get('journal_entry') and r.get('transaction_type') == 'Invoice Application'}
    for name, journal in journals.items():
        if journal.get('qas_store_credit_invoice') and amount(journal.get('qas_store_credit_amount')) > 0:
            credit_journals[name] = journal['qas_store_credit_invoice']
    settlement = {name: {'cash': amount(0), 'credit': amount(0), 'refund': amount(0), 'sources': []} for name in relevant}
    # Aggregate per voucher first. A voucher may have multiple ledger rows, but
    # only its net active allocation represents settlement of this invoice.
    grouped = {}
    for row in ledger:
        name = row['against_voucher_no']
        if row.get('delinked') or name not in relevant:
            continue
        if row.get('account_currency') != 'AUD':
            issue(returns.get(name, {}).get('return_against') or name, 'unsupported_currency')
            continue
        key = (name, row['voucher_type'], row['voucher_no'])
        grouped[key] = grouped.get(key, amount(0)) - amount(row.get('amount_in_account_currency'))
    for (name, kind, voucher), value in grouped.items():
        original = returns.get(name, {}).get('return_against') or name
        bucket = settlement[name]
        if kind == 'Sales Invoice':
            if voucher != name and voucher not in returns:
                issue(original, 'unclassified_settlement')
            continue  # Face value and returns are not cash/credit payments.
        if not value:
            continue
        if kind == 'Payment Entry':
            payment = payments.get(voucher, {})
            if payment.get('docstatus') != 1:
                issue(original, 'settlement_mismatch')
                continue
            if voucher in payment_with_deductions or amount(payment.get('difference_amount')):
                issue(original, 'payment_deductions')
                continue
            key = 'cash' if value > 0 else 'refund'
        elif kind == 'Journal Entry':
            journal = journals.get(voucher, {})
            if journal.get('docstatus') != 1:
                issue(original, 'settlement_mismatch')
                continue
            if credit_journals.get(voucher) != name:
                issue(original, 'unclassified_settlement')
                continue
            key = 'credit' if value > 0 else 'refund'
        else:
            issue(original, 'unclassified_settlement')
            continue
        bucket[key] += abs(value)
        bucket['sources'].append({'type': kind, 'name': voucher, 'component': key, 'amount': float(abs(value))})
    for row in credit_ledger:
        original = returns.get(row['invoice'], {}).get('return_against') or row['invoice']
        if row.get('transaction_type') == 'Invoice Application' and amount(row.get('debit_amount')) and not row.get('journal_entry'):
            # Legacy applications need review instead of trusting a stale snapshot.
            matching = any(j.get('qas_store_credit_invoice') == row['invoice'] and j.get('docstatus') == 1 for j in journals.values())
            if not matching:
                issue(original, 'unverified_credit')
        if row.get('transaction_type') in ('Correction', 'Withdrawal Credit') and amount(row.get('credit_amount')):
            issue(original, 'credit_adjustment')
    items = []
    for name in sorted(selected):
        header, invoice_lines = headers[name], sorted(by_invoice[name], key=lambda r: (r.get('idx') or 0, r['name']))
        bucket = settlement[name]
        if header.get('currency') != 'AUD':
            issue(name, 'unsupported_currency')
            continue
        if header.get('is_pos'):
            pos = max(amount(0), amount(header.get('paid_amount')) - amount(header.get('change_amount')))
            bucket['cash'] += pos
            if pos:
                bucket['sources'].append({'type': 'Sales Invoice', 'name': name, 'component': 'cash', 'amount': float(pos)})
        returned = {}
        invoice_returns = [r for r in returns.values() if r['return_against'] == name]
        if invoice_returns and len([r for r in invoice_lines if amount(r.get('net_amount') or r.get('amount'))]) > 1 and bucket['cash'] + bucket['credit'] < _total(header):
            # Returning a specific line after partial settlement changes the
            # allocation of unpaid fees. Do not invent a refund across courses.
            issue(name, 'mixed_course_return')
        for ret in invoice_returns:
            if ret.get('currency') != 'AUD':
                issue(name, 'unsupported_currency')
                continue
            ret_lines = by_invoice.get(ret['name'], [])
            ret_total = abs(_total(ret))
            weights = [abs(amount(r.get('net_amount') if r.get('net_amount') is not None else r.get('amount'))) for r in ret_lines]
            parts = allocate(ret_total, weights)
            for row, value in zip(ret_lines, parts):
                original_line = row.get('sales_invoice_item')
                matches = [r for r in invoice_lines if r['name'] == original_line] if original_line else [r for r in invoice_lines if r.get('course') == row.get('course') and r.get('term') == row.get('term') and r.get('student') == row.get('student')]
                if len(matches) != 1:
                    issue(name, 'unmatched_return')
                    continue
                key = matches[0]['name']
                returned[key] = returned.get(key, amount(0)) + value
            # Combine real refunds with return caps using max, not addition,
            # so the credit note and its refund remove the fee only once.
            bucket['refund'] += settlement[ret['name']]['refund']
            bucket['sources'].extend(settlement[ret['name']]['sources'])
            if settlement[ret['name']]['cash'] or settlement[ret['name']]['credit']:
                issue(name, 'settlement_mismatch')
            if not ret_lines or sum(parts) != ret_total:
                issue(name, 'unmatched_return')
        total = _total(header)
        if invoice_returns and bucket['refund'] > sum(returned.values(), amount(0)):
            issue(name, 'unmatched_return')
        if bucket['refund'] > bucket['cash'] + bucket['credit'] or sum(returned.values(), amount(0)) > total:
            issue(name, 'settlement_mismatch')
        if any(not r.get('course') and r.get('qas_line_type') != 'Other' for r in invoice_lines):
            issue(name, 'missing_attribution')
        # Current outstanding is a reconciliation check only, never a payment source.
        ledger_net = sum((value for (inv, kind, voucher), value in grouped.items() if inv == name), amount(0))
        if abs(-ledger_net - amount(header.get('outstanding_amount'))) > amount('0.02'):
            issue(name, 'settlement_mismatch')
        if any(d['invoice'] == name for d in diagnostics):
            continue
        try:
            allocated = invoice_allocations(total, invoice_lines, bucket['cash'], bucket['credit'], bucket['refund'], returned)
        except ValueError:
            issue(name, 'unsupported_charges')
            continue
        for row in allocated:
            if row.get('term') != term or row.get('course') != course:
                continue
            items.append({**row, 'invoice': name, 'posting_date': str(header.get('posting_date') or ''),
                          'invoice_total': float(total), 'invoice_cash': float(bucket['cash']),
                          'invoice_credit': float(bucket['credit']), 'sources': bucket['sources']})
    fields = ['billed_amount', 'cash_received', 'credit_used', 'reductions', 'eligible_revenue']
    summary = {key: float(sum((amount(r[key]) for r in items), amount(0))) for key in fields}
    summary['invoice_count'] = len({r['invoice'] for r in items})
    summary['line_count'] = len(items)
    return {'items': items, 'summary': summary, 'diagnostics': diagnostics, 'complete': not diagnostics}


def _total(header):
    rounded = header.get('rounded_total')
    return amount(rounded if rounded and not header.get('disable_rounded_total') else header.get('grand_total'))
