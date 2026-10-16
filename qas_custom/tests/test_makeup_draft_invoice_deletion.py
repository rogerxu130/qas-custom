from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import school_admin


class TestMakeupDraftInvoiceDeletion(TestCase):
    def test_clears_only_invoice_links_and_records_audit(self):
        fake = SimpleNamespace(
            db=SimpleNamespace(has_column=Mock(return_value=True), set_value=Mock()),
            get_all=Mock(return_value=['MV-001', 'MV-002']),
        )
        with patch.object(school_admin, 'frappe', fake), patch.object(
            school_admin, '_add_comment'
        ) as audit, patch.object(school_admin, '_', side_effect=lambda text: text):
            school_admin._detach_draft_invoice_makeup_voucher_links('INV')
        fake.get_all.assert_called_once_with(
            'Makeup Voucher', filters={'price_difference_invoice': 'INV'},
            pluck='name', limit_page_length=0,
        )
        self.assertEqual(fake.db.set_value.call_count, 2)
        for voucher in ['MV-001', 'MV-002']:
            fake.db.set_value.assert_any_call('Makeup Voucher', voucher, 'price_difference_invoice', None)
            audit.assert_any_call('Makeup Voucher', voucher,
                'Draft difference invoice INV was deleted and its invoice link cleared.')

    def test_missing_invoice_column_skips_cleanup(self):
        fake = SimpleNamespace(db=SimpleNamespace(has_column=Mock(return_value=False)), get_all=Mock())
        with patch.object(school_admin, 'frappe', fake):
            school_admin._detach_draft_invoice_makeup_voucher_links('INV')
        fake.get_all.assert_not_called()

    def run_delete(self, *, docstatus=0, deletion_error=None):
        events = []
        fake = SimpleNamespace(
            get_doc=Mock(return_value=SimpleNamespace(name='INV', docstatus=docstatus)),
            delete_doc=Mock(side_effect=deletion_error or (lambda *a, **kw: events.append('delete'))),
            throw=Mock(side_effect=ValueError('Only draft invoices can be deleted')),
            db=SimpleNamespace(savepoint=Mock(), rollback=Mock(), commit=Mock()),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(school_admin, 'frappe', fake))
            stack.enter_context(patch.object(school_admin, '_', side_effect=lambda text: text))
            for name, value in {
                '_require_invoice_cancellation_actor': None,
                'lock_payg_operations_for_invoices': {},
                'reject_payg_support_view_write': None,
                '_detach_invoice_operation_report_links': None,
                '_clear_deleted_invoice_enrollment_snapshot': None,
            }.items():
                stack.enter_context(patch.object(school_admin, name, return_value=value))
            detach = stack.enter_context(patch.object(
                school_admin, '_detach_draft_invoice_makeup_voucher_links',
                side_effect=lambda invoice: events.append('detach'),
            ))
            if docstatus != 0 or deletion_error:
                with self.assertRaises(ValueError):
                    school_admin.delete_school_admin_draft_invoice_data(invoice='INV')
            else:
                self.assertEqual(school_admin.delete_school_admin_draft_invoice_data(invoice='INV'), {'deleted': 'INV'})
        return fake, detach, events

    def test_direct_delete_detaches_before_deleting(self):
        fake, detach, events = self.run_delete()
        self.assertEqual(events, ['detach', 'delete'])
        detach.assert_called_once_with('INV')
        fake.delete_doc.assert_called_once_with('Sales Invoice', 'INV', ignore_permissions=True)
        fake.db.commit.assert_called_once()
        fake.db.rollback.assert_not_called()

    def test_submitted_invoice_cannot_be_detached_or_deleted(self):
        fake, detach, _ = self.run_delete(docstatus=1)
        detach.assert_not_called()
        fake.delete_doc.assert_not_called()
        fake.db.commit.assert_not_called()

    def test_failed_delete_rolls_back_link_cleanup(self):
        fake, _, _ = self.run_delete(deletion_error=ValueError('another protected link'))
        fake.db.rollback.assert_called_once_with(save_point='school_admin_delete_draft_invoice')
        fake.db.commit.assert_not_called()

    def test_makeup_cancellation_detaches_before_deleting_only_drafts(self):
        events = []
        fake = SimpleNamespace(delete_doc=Mock(side_effect=lambda *a, **kw: events.append('delete')))
        with patch.object(school_admin, 'frappe', fake), patch.object(
            school_admin, '_detach_draft_invoice_makeup_voucher_links',
            side_effect=lambda invoice: events.append('detach'),
        ) as detach, patch.object(school_admin, '_run_school_admin_invoice_mutation', side_effect=lambda callback: callback()):
            school_admin._delete_makeup_difference_draft({'name': 'INV', 'docstatus': 0})
            school_admin._delete_makeup_difference_draft({'name': 'SUBMITTED', 'docstatus': 1})
        detach.assert_called_once_with('INV')
        self.assertEqual(events, ['detach', 'delete'])
