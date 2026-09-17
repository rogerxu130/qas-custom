from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services.school_admin import reopen_school_admin_unpaid_invoice_data
from qas_custom.modules.notifications.commands import _invoice_notification_event_key

MODULE='qas_custom.services.school_admin'


class TestSameNumberReopen(TestCase):
    def test_same_document_returned_and_relations_refreshed_without_new_invoice(self):
        doc=frappe._dict(name='2600010', docstatus=1, outstanding_amount=68, grand_total=68)
        draft=frappe._dict(name=doc.name, docstatus=0)
        db=Mock()
        with patch(MODULE+'._require_school_admin'), patch(MODULE+'.payment_mutations_enabled',return_value=True), patch(MODULE+'.has_active_payment_plan',return_value=False), patch(MODULE+'._reopen_unpaid_invoice_safety_check'), patch(MODULE+'.get_invoice_total_amount',return_value=68), patch(MODULE+'.frappe.db',db), patch(MODULE+'.frappe.get_doc',return_value=doc), patch(MODULE+'._run_school_admin_invoice_mutation',side_effect=lambda fn:fn()), patch('qas_custom.modules.billing.invoice_reopen.reopen_same_invoice',return_value=draft) as reopen, patch(MODULE+'._move_enrollment_invoice_snapshots_to_amendment') as links, patch(MODULE+'._build_invoice_payload',return_value={'name':doc.name,'docstatus':0}), patch(MODULE+'.frappe.copy_doc') as copy:
            result=reopen_school_admin_unpaid_invoice_data(doc.name,'Adjust discount')
        self.assertEqual(result['name'],doc.name)
        reopen.assert_called_once_with(doc,'Adjust discount')
        links.assert_called_once_with(draft,draft,'Adjust discount')
        copy.assert_not_called()
        db.commit.assert_called_once()

    def test_reversal_failure_rolls_back_without_changing_identity(self):
        doc=frappe._dict(name='I1',docstatus=1,outstanding_amount=68)
        db=Mock()
        with patch(MODULE+'._require_school_admin'), patch(MODULE+'.payment_mutations_enabled',return_value=True), patch(MODULE+'.has_active_payment_plan',return_value=False), patch(MODULE+'._reopen_unpaid_invoice_safety_check'), patch(MODULE+'.get_invoice_total_amount',return_value=68), patch(MODULE+'.frappe.db',db), patch(MODULE+'.frappe.get_doc',return_value=doc), patch(MODULE+'._run_school_admin_invoice_mutation',side_effect=RuntimeError('Cannot reverse ledger')):
            with self.assertRaises(RuntimeError):
                reopen_school_admin_unpaid_invoice_data('I1','Correction')
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with(save_point='school_admin_reopen_invoice')

    def test_paid_and_store_credit_checks_still_block_reopen(self):
        from qas_custom.services.school_admin import _reopen_unpaid_invoice_safety_check
        doc=frappe._dict(name='I1',docstatus=1)
        for paid,credit,journal in [(10,0,False),(0,10,False),(0,0,True)]:
            with self.subTest(paid=paid,credit=credit,journal=journal), patch(MODULE+'._invoice_payment_amount',return_value=paid), patch(MODULE+'.get_invoice_store_credit_applied',return_value=credit), patch(MODULE+'.has_invoice_store_credit_journal_entry',return_value=journal), patch(MODULE+'.frappe.throw',side_effect=frappe.ValidationError):
                with self.assertRaises(frappe.ValidationError):
                    _reopen_unpaid_invoice_safety_check(doc)

    def test_approval_email_dedup_is_per_reopen_revision(self):
        doc=frappe._dict(name='I1')
        with patch('qas_custom.modules.billing.invoice_reopen.reopened_invoice_revision',return_value=None):
            self.assertEqual(_invoice_notification_event_key(doc,'approved'),'invoice_approved:I1')
        with patch('qas_custom.modules.billing.invoice_reopen.reopened_invoice_revision',return_value='COMMENT1'):
            first=_invoice_notification_event_key(doc,'approved')
            self.assertEqual(_invoice_notification_event_key(doc,'approved'),first)
        with patch('qas_custom.modules.billing.invoice_reopen.reopened_invoice_revision',return_value='COMMENT2'):
            self.assertNotEqual(_invoice_notification_event_key(doc,'approved'),first)
