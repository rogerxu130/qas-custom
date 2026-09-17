from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.modules.billing.trial_invoice_dates import sync_trial_invoice_dates, repair_trial_invoice_dates
from qas_custom.services.trial_invoice import _trial_invoice_draft_payload

MODULE = 'qas_custom.modules.billing.trial_invoice_dates'


class Document(frappe._dict):
    def db_set(self, field, value, **kwargs):
        self[field] = value


class TestTrialInvoiceDates(TestCase):
    def setUp(self):
        self.invoice = Document(name='INV1', docstatus=1, posting_date='2026-09-01',
            due_date='2026-09-08', outstanding_amount=68,
            payment_schedule=[Document(due_date='2026-09-08')],
            set_status=Mock(), add_comment=Mock())
        self.inquiry = frappe._dict(name='INQ1', inquiry_type='Trial Lesson',
                                   status='Booked', course_session='SESSION1')
        self.db = Mock()
        self.db.get_value.return_value = '2026-10-12'
        self.db_patch = patch(MODULE+'.frappe.db', self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def test_corrects_invoice_schedule_ledgers_and_status(self):
        result = sync_trial_invoice_dates(self.invoice, self.inquiry)
        self.assertEqual(result['due_date'], '2026-10-05')
        self.assertEqual(str(self.invoice.due_date), '2026-10-05')
        self.assertEqual(str(self.invoice.payment_schedule[0].due_date), '2026-10-05')
        self.assertEqual(self.db.set_value.call_count, 2)
        self.invoice.set_status.assert_called_once_with(update=True)
        self.invoice.add_comment.assert_called_once()
        self.assertIsNone(sync_trial_invoice_dates(self.invoice, self.inquiry))
        self.invoice.add_comment.assert_called_once()

    def test_preview_does_not_write(self):
        self.assertEqual(sync_trial_invoice_dates(self.invoice, self.inquiry, dry_run=True)['due_date'], '2026-10-05')
        self.assertEqual(self.invoice.due_date, '2026-09-08')
        self.db.set_value.assert_not_called()
        self.invoice.add_comment.assert_not_called()

    def test_late_booking_and_rescheduling_use_original_posting_date(self):
        self.db.get_value.return_value = '2026-09-04'
        self.assertEqual(sync_trial_invoice_dates(self.invoice, self.inquiry)['due_date'], '2026-09-04')
        self.inquiry.status = 'Rescheduled'
        self.db.get_value.return_value = '2026-11-02'
        self.assertEqual(sync_trial_invoice_dates(self.invoice, self.inquiry)['due_date'], '2026-10-26')

    def test_protected_invoices_are_unchanged(self):
        for fields in ({'outstanding_amount': 0}, {'docstatus': 2}, {'is_return': 1},
                       {'qas_has_payment_plan': 1}, {'payment_schedule': [Document(), Document()]}):
            with self.subTest(fields=fields):
                invoice = Document(self.invoice)
                invoice.update(fields)
                self.assertIsNone(sync_trial_invoice_dates(invoice, self.inquiry))
        self.db.set_value.assert_not_called()

    def test_missing_session_is_skipped(self):
        self.db.get_value.return_value = None
        self.assertIsNone(sync_trial_invoice_dates(self.invoice, self.inquiry))
        self.db.set_value.assert_not_called()

    def test_draft_repaired_without_ledger_or_status_updates(self):
        self.invoice.docstatus = 0
        sync_trial_invoice_dates(self.invoice, self.inquiry)
        self.db.set_value.assert_not_called()
        self.invoice.set_status.assert_not_called()
        self.assertEqual(str(self.invoice.due_date), '2026-10-05')

    def test_repair_defaults_to_preview(self):
        with patch(MODULE+'.frappe.get_all', return_value=[frappe._dict(name='INV1', source_document='INQ1')]), patch(
            MODULE+'.frappe.get_doc', side_effect=[self.invoice, self.inquiry]):
            self.assertEqual(len(repair_trial_invoice_dates()), 1)
        self.assertEqual(self.invoice.due_date, '2026-09-08')

    def test_migration_supports_production_schema_without_source_type(self):
        from qas_custom.patches.v2026_09_17_correct_trial_invoice_due_dates import execute
        def query(doctype, *, filters, fields, limit_page_length):
            self.assertEqual(doctype, 'Sales Invoice')
            self.assertNotIn('source_type', filters)
            self.assertNotIn('source_type', fields)
            self.assertEqual(filters['source_doctype'], 'Inquiry')
            return [frappe._dict(name='INV1', source_document='INQ1')]
        with patch(MODULE+'.frappe.get_all', side_effect=query), patch(
            MODULE+'.frappe.get_doc', side_effect=[self.invoice, self.inquiry]):
            execute()
        self.assertEqual(str(self.invoice.due_date), '2026-10-05')
        self.invoice.set_status.assert_called_once_with(update=True)

    def test_non_trial_inquiry_invoice_is_not_changed(self):
        self.inquiry.inquiry_type = 'School Visit'
        with patch(MODULE+'.frappe.get_all', return_value=[frappe._dict(name='INV1', source_document='INQ1')]), patch(
            MODULE+'.frappe.get_doc', side_effect=[self.invoice, self.inquiry]):
            self.assertEqual(repair_trial_invoice_dates(dry_run=False), [])
        self.assertEqual(self.invoice.due_date, '2026-09-08')
        self.db.set_value.assert_not_called()
        self.invoice.add_comment.assert_not_called()

    def test_new_and_replacement_payload_use_lesson_date(self):
        module='qas_custom.services.trial_invoice'
        inquiry=frappe._dict(name='INQ1', parent='P1', student='S1', course_session='SESSION1')
        context={'session': frappe._dict(session_date='2026-10-12'),
                 'timeslot': frappe._dict(start_time='09:00', end_time='10:00'),
                 'course':'Art', 'customer':'C1', 'fee':68, 'item_code':'ART'}
        with patch(module+'.nowdate', return_value='2026-09-01'), patch(module+'._', side_effect=lambda x:x), patch(
            module+'.formatdate', side_effect=lambda x:x), patch(module+'.format_time', side_effect=lambda x:x), patch(
            module+'.get_student_parent_name', return_value='Student'), patch(module+'.get_student_display_code', return_value='S1'), patch(
            module+'.get_course_session_snapshot_label', return_value='Lesson'), patch(module+'.referral_invoice_message', return_value=''):
            for replacement in (False, True):
                payload=_trial_invoice_draft_payload(inquiry, context, replacement=replacement)
                self.assertEqual(str(payload['due_date']), '2026-10-05')
