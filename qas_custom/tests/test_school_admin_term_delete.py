from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from qas_custom.services import school_admin as service


class TestSchoolAdminTermDelete(TestCase):
    def run_delete(self, snapshots=(), business_error=None, delete_error=None):
        db = Mock()
        fake = SimpleNamespace(
            db=db, get_all=Mock(return_value=list(snapshots)),
            delete_doc=Mock(side_effect=delete_error),
            throw=Mock(side_effect=ValueError),
        )
        with patch.object(service, 'frappe', fake), \
             patch.object(service, '_', side_effect=lambda text: text), \
             patch.object(service, '_require_school_admin'), \
             patch.object(service, '_assert_safe_delete', side_effect=business_error), \
             patch.object(service, '_doctype_available', return_value=True):
            try:
                result = service.delete_school_admin_term_data('Test Term')
            except ValueError:
                result = None
        return fake, result

    def test_completed_and_failed_snapshots_are_removed_before_term(self):
        snapshots = [SimpleNamespace(name='snapshot-1', status='Completed'),
                     SimpleNamespace(name='snapshot-2', status='Failed')]
        fake, result = self.run_delete(snapshots)
        self.assertEqual(result, {'deleted': 'Test Term'})
        fake.get_all.assert_called_once_with('QAS Admin Report Snapshot',
            filters={'term': 'Test Term'}, fields=['name', 'status'], limit_page_length=0)
        self.assertEqual(fake.db.delete.call_args_list, [
            call('QAS Admin Report Row', {'snapshot': 'snapshot-1'}),
            call('QAS Admin Report Row', {'snapshot': 'snapshot-2'})])
        self.assertEqual(fake.delete_doc.call_args_list, [
            call('QAS Admin Report Snapshot', 'snapshot-1', ignore_permissions=True),
            call('QAS Admin Report Snapshot', 'snapshot-2', ignore_permissions=True),
            call('Term', 'Test Term', ignore_permissions=True)])
        fake.db.commit.assert_called_once()
        fake.db.rollback.assert_not_called()

    def test_pending_report_blocks_all_deletes(self):
        for status in ['Queued', 'Running']:
            with self.subTest(status=status):
                fake, result = self.run_delete([
                    SimpleNamespace(name='old', status='Completed'),
                    SimpleNamespace(name='pending', status=status)])
                self.assertIsNone(result)
                fake.db.delete.assert_not_called()
                fake.delete_doc.assert_not_called()
                fake.db.commit.assert_not_called()

    def test_business_records_block_snapshot_cleanup(self):
        fake, result = self.run_delete(business_error=ValueError('Enrollment exists'))
        self.assertIsNone(result)
        fake.get_all.assert_not_called()
        fake.db.delete.assert_not_called()
        fake.delete_doc.assert_not_called()
        fake.db.commit.assert_not_called()

    def test_remaining_link_failure_rolls_back_snapshot_cleanup(self):
        fake, result = self.run_delete(
            [SimpleNamespace(name='snapshot', status='Completed')],
            delete_error=[None, ValueError('Other linked business record')])
        self.assertIsNone(result)
        fake.db.delete.assert_called_once()
        fake.db.rollback.assert_called_once_with(save_point='delete_school_admin_term')
        fake.db.commit.assert_not_called()

    def test_empty_term_still_deletes(self):
        fake, result = self.run_delete()
        self.assertEqual(result, {'deleted': 'Test Term'})
        fake.delete_doc.assert_called_once_with('Term', 'Test Term', ignore_permissions=True)
        fake.db.commit.assert_called_once()
