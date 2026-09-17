from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.modules.billing.invoice_corrections import change_submitted_due_date, amendment_target, repair_trial_amendment_links, link_trial_amendment
from qas_custom.services.school_admin import _attach_trial_payment_status

MODULE = 'qas_custom.modules.billing.invoice_corrections'


def invoice(name, parent=None, state=2, **kwargs):
    return frappe._dict(name=name, amended_from=parent, docstatus=state, source_doctype='Inquiry', source_document='INQ1', **kwargs)


class TestInvoiceCorrections(TestCase):
    def test_submitted_date_edit_preserves_identity_money_and_links(self):
        doc=invoice('2600010', '2600009', 1, due_date='2026-09-07', posting_date='2026-09-01',
                    grand_total=68, outstanding_amount=18, payment_schedule=[frappe._dict(name='PAY1', due_date='2026-09-07', payment_amount=68)])
        doc.set_status=Mock()
        doc.add_comment=Mock()
        with patch(MODULE+'.frappe.db', Mock()) as db, patch(MODULE+'.frappe.get_doc', return_value=doc):
            result=change_submitted_due_date(doc.name, '2026-09-29')
        self.assertIs(result, doc)
        self.assertEqual(doc.name, '2600010')
        self.assertEqual(doc.docstatus, 1)
        self.assertEqual(doc.source_document, 'INQ1')
        self.assertEqual((doc.grand_total, doc.outstanding_amount), (68,18))
        self.assertEqual(str(doc.due_date), '2026-09-29')
        self.assertEqual(str(doc.payment_schedule[0].due_date), '2026-09-29')
        self.assertEqual([c.args[0] for c in db.set_value.call_args_list], ['Sales Invoice','Payment Schedule','GL Entry','Payment Ledger Entry'])
        self.assertTrue(all(c.args[2]=='due_date' for c in db.set_value.call_args_list))
        doc.set_status.assert_called_once_with(update=True)

    def test_rejects_installments_returns_cancelled_and_invalid_dates_without_writes(self):
        for override in ({'qas_has_payment_plan':1}, {'payment_schedule':[{},{}]}, {'docstatus':2}, {'is_return':1}, {'posting_date':'2026-10-01'}):
            with self.subTest(override=override):
                doc=frappe._dict(name='I1', docstatus=1, posting_date='2026-09-01', payment_schedule=[])
                doc.update(override)
                with patch(MODULE+'.frappe.db', Mock()) as db, patch(MODULE+'.frappe.get_doc', return_value=doc), patch(MODULE+'.frappe.throw', side_effect=frappe.ValidationError):
                    with self.assertRaises(frappe.ValidationError):
                        change_submitted_due_date('I1', '2026-09-29')
                    db.set_value.assert_not_called()

    def test_follows_multiple_amendments_to_paid_invoice(self):
        rows=[invoice('old'), invoice('middle','old'), invoice('new','middle',1, outstanding_amount=0)]
        self.assertEqual(amendment_target({'name':'INQ1','trial_invoice':'old'}, {r.name:r for r in rows}), ('new',None))

    def test_does_not_guess_ambiguous_or_wrong_source(self):
        inquiry={'name':'INQ1','trial_invoice':'old'}
        rows=[invoice('old'), invoice('a','old',1), invoice('b','old',0)]
        self.assertIsNone(amendment_target(inquiry,{r.name:r for r in rows})[0])
        rows[-1].source_document='OTHER'
        self.assertIsNone(amendment_target(inquiry,{r.name:r for r in rows})[0])

    def test_valid_current_link_and_unrelated_invoice_remain_unchanged(self):
        rows=[invoice('old',state=1),invoice('unrelated',state=1)]
        self.assertEqual(amendment_target({'name':'INQ1','trial_invoice':'old'},{r.name:r for r in rows}), (None,None))

    def test_repair_links_paid_invoice_and_badge_reads_its_balance(self):
        inquiry=frappe._dict(name='INQ1', trial_invoice='old')
        rows=[invoice('old'),invoice('new','old',1, outstanding_amount=0, status='Paid')]
        db=Mock()
        db.sql.return_value=[frappe._dict(trial_invoice='old')]
        db.exists.return_value=False
        def set_value(doctype,name,field,value):
            self.assertEqual((doctype,name,field),('Inquiry','INQ1','trial_invoice'))
            inquiry.trial_invoice=value
        db.set_value.side_effect=set_value
        audit=Mock()
        with patch(MODULE+'.frappe.db',db), patch(MODULE+'.frappe.get_all',side_effect=lambda kind,**kw:[inquiry] if kind=='Inquiry' else rows), patch(MODULE+'.frappe.get_doc',return_value=audit):
            preview=repair_trial_amendment_links()
            db.set_value.assert_not_called()
            self.assertEqual(preview['changes'][0]['invoice'],'new')
            result=repair_trial_amendment_links(dry_run=False)
            self.assertEqual(inquiry.trial_invoice,'new')
            db.set_value.assert_called_once()
            db.sql.return_value=[frappe._dict(trial_invoice='new')]
            self.assertEqual(repair_trial_amendment_links(dry_run=False)['changes'],[])
        item={'name':'INQ1','inquiry_type':'Trial Lesson','trial_invoice':inquiry.trial_invoice}
        with patch('qas_custom.services.school_admin.frappe.get_all',return_value=[rows[-1]]):
            _attach_trial_payment_status([item])
        self.assertEqual(item['trial_payment_status'],'paid')
        self.assertEqual(item['trial_invoice'],'new')
        self.assertEqual(rows[-1].outstanding_amount,0)

    def test_future_reopen_invokes_same_repair_in_transaction(self):
        with patch(MODULE+'.repair_trial_amendment_links',return_value={'changes':[],'review':[]}) as repair:
            link_trial_amendment(invoice('old'),invoice('new','old',0))
            repair.assert_called_once_with(dry_run=False,inquiry_name='INQ1')
