"""Date-only corrections and conservative trial amendment link recovery."""
import frappe
from frappe.utils import cint, getdate


def change_submitted_due_date(invoice_name, due_date):
    if not due_date:
        frappe.throw('A payment due date is required.')
    due_date = getdate(due_date)
    frappe.db.sql('select name from `tabSales Invoice` where name=%s for update', (invoice_name,))
    doc = frappe.get_doc('Sales Invoice', invoice_name)
    if cint(doc.docstatus) != 1 or doc.get('is_return'):
        frappe.throw('Only submitted, non-return invoices support this date correction.')
    schedule = doc.get('payment_schedule') or []
    if cint(doc.get('qas_has_payment_plan')) or len(schedule) > 1:
        frappe.throw('This invoice has installments. Edit its payment plan instead.')
    if due_date < getdate(doc.posting_date):
        frappe.throw('Payment due date cannot be before the invoice posting date.')
    old_date = doc.get('due_date')
    doc.due_date = due_date
    frappe.db.set_value('Sales Invoice', doc.name, 'due_date', due_date)
    for row in schedule:
        row.due_date = due_date
        frappe.db.set_value('Payment Schedule', row.name, 'due_date', due_date)
    for doctype in ('GL Entry', 'Payment Ledger Entry'):
        frappe.db.set_value(doctype, {'voucher_type': 'Sales Invoice', 'voucher_no': doc.name},
                            'due_date', due_date, update_modified=False)
    doc.set_status(update=True)
    doc.add_comment('Info', f'Payment due date changed from {old_date} to {due_date}; invoice number and payment records retained.')
    return doc


def amendment_target(inquiry, invoices):
    """Only follow explicit amendment ancestry; never guess by customer or amount."""
    original = invoices.get(inquiry.get('trial_invoice'))
    if not original or cint(original.get('docstatus')) != 2:
        return None, None
    children = {}
    for row in invoices.values():
        if row.get('amended_from'):
            children.setdefault(row['amended_from'], []).append(row)
    pending = [original['name']]
    visited = set()
    active = []
    while pending:
        name = pending.pop()
        if name in visited:
            return None, 'Cyclic amendment history'
        visited.add(name)
        for row in children.get(name, []):
            if row.get('source_doctype') != 'Inquiry' or row.get('source_document') != inquiry['name']:
                return None, 'Amendment source does not match Inquiry'
            pending.append(row['name'])
            if cint(row.get('docstatus')) != 2:
                active.append(row['name'])
    if len(active) > 1:
        return None, 'Multiple active amendments; manual review required'
    return (active[0], None) if active else (None, None)


def repair_trial_amendment_links(*, dry_run=True, inquiry_name=None):
    filters = {'inquiry_type': 'Trial Lesson', 'trial_invoice': ['is', 'set']}
    if inquiry_name:
        filters['name'] = inquiry_name
    inquiries = frappe.get_all('Inquiry', filters=filters, fields=['name', 'trial_invoice'], limit_page_length=0)
    if not inquiries:
        return {'changes': [], 'review': []}
    rows = frappe.get_all('Sales Invoice', filters={'is_return': 0},
        fields=['name', 'amended_from', 'docstatus', 'source_doctype', 'source_document'], limit_page_length=0)
    invoices = {row.name: row for row in rows}
    result = {'changes': [], 'review': []}
    for inquiry in inquiries:
        if not dry_run:
            locked = frappe.db.sql('select trial_invoice from `tabInquiry` where name=%s for update',
                                  (inquiry.name,), as_dict=True)
            if not locked or locked[0].trial_invoice != inquiry.trial_invoice:
                result['review'].append({'inquiry': inquiry.name, 'reason': 'Link changed during repair'})
                continue
        target, reason = amendment_target(inquiry, invoices)
        if reason:
            result['review'].append({'inquiry': inquiry.name, 'reason': reason})
        if not target:
            continue
        if frappe.db.exists('Inquiry', {'trial_invoice': target, 'name': ['!=', inquiry.name]}):
            result['review'].append({'inquiry': inquiry.name, 'reason': 'Amendment is linked to another Inquiry'})
            continue
        result['changes'].append({'inquiry': inquiry.name, 'old_invoice': inquiry.trial_invoice, 'invoice': target})
        if not dry_run:
            frappe.db.set_value('Inquiry', inquiry.name, 'trial_invoice', target)
            frappe.get_doc('Inquiry', inquiry.name).add_comment('Info',
                f'Trial invoice link corrected from {inquiry.trial_invoice} to amendment {target}. Payment records unchanged.')
    return result


def link_trial_amendment(original, amendment):
    if original.get('source_doctype') != 'Inquiry' or not original.get('source_document'):
        return
    result = repair_trial_amendment_links(dry_run=False, inquiry_name=original.source_document)
    if result['review']:
        frappe.throw('The trial invoice link needs review; the invoice correction was not completed.')
