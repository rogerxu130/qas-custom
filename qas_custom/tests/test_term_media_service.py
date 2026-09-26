from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import term_media as service


class TestTermMediaService(TestCase):
    def setUp(self):
        self.mock = Mock()
        self.mock._dict = lambda **kwargs: SimpleNamespace(**kwargs)
        def fail(message, *args):
            raise ValueError(message)
        self.mock.throw.side_effect = fail
        self.patcher = patch.object(service, 'frappe', self.mock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.row = dict(key='key', kind='photo', post='P1', idx=1, row='CH1', term='T1', timeslot='W1', session='S1',
                        file_id='F1', url='/private/files/image.jpg', status='exported', bytes=123, sha256='hash')

    def test_failed_export_not_candidate(self):
        self.mock.db.get_value.return_value = None
        with patch.object(service, '_source') as source:
            result = service._candidate(dict(self.row, status='failed'), [])
        source.assert_not_called()
        self.assertEqual(result['reason'], 'not_archived')

    def test_shared_file_is_never_ready(self):
        self.mock.db.get_value.return_value = None
        with patch.object(service, '_source'), patch.object(service, '_shared', return_value=True):
            result = service._candidate(self.row, [])
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'shared_file')

    def test_changed_content_is_never_ready(self):
        self.mock.db.get_value.return_value = None
        with patch.object(service, '_source', side_effect=ValueError('file_changed')):
            result = service._candidate(self.row, [])
        self.assertEqual(result['status'], 'skipped')

    def test_recovery_does_not_clear_new_attachment(self):
        self.mock.db.get_value.return_value = 'Removing'
        with patch.object(service, '_term'), patch.object(service, '_assert_eligible'), patch.object(service, '_lock_source'), \
             patch.object(service, '_source_link_matches', return_value=False), patch.object(service, '_clear_source') as clear:
            result = service._remove_one('A1', self.row, 'admin', [])
        self.assertEqual(result['status'], 'skipped')
        clear.assert_not_called()
        self.mock.delete_doc.assert_not_called()

    def test_unbacked_cleanup_is_rejected_before_enqueue(self):
        doc = SimpleNamespace(term='T1', backup_confirmed_at=None, reload=lambda:None)
        self.mock.get_doc.return_value = doc
        with patch.object(service, 'require_admin'), patch.object(service, '_term'), patch.object(service, '_lock'), \
             patch.object(service, '_assert_eligible'):
            with self.assertRaisesRegex(ValueError, 'backup_not_confirmed'):
                service.start_cleanup('A1', confirmation='T1')
        self.mock.enqueue.assert_not_called()

    def test_wrong_term_confirmation_rejected(self):
        doc = SimpleNamespace(term='T1', backup_confirmed_at='2026-01-01', reload=lambda:None)
        self.mock.get_doc.return_value = doc
        with patch.object(service, 'require_admin'), patch.object(service, '_term'), patch.object(service, '_lock'), \
             patch.object(service, '_assert_eligible'):
            with self.assertRaisesRegex(ValueError, 'term_confirmation_required'):
                service.start_cleanup('A1', confirmation='Other term')
        self.mock.enqueue.assert_not_called()

    def test_clear_source_does_not_clear_changed_link(self):
        with patch.object(service, '_source_link_matches', return_value=False):
            with self.assertRaisesRegex(ValueError, 'membership_changed'):
                service._clear_source(self.row)
        self.mock.db.set_value.assert_not_called()

    def test_term_edit_blocked_during_cleanup(self):
        self.mock.db.exists.return_value = True
        self.mock.db.get_value.return_value = SimpleNamespace(status='Archived', end_date='2026-09-20')
        doc = SimpleNamespace(name='T1', status='Active', end_date='2026-09-20', is_new=lambda:False)
        with patch.object(service, '_lock'):
            with self.assertRaisesRegex(ValueError, 'cleanup is in progress'):
                service.validate_term_change(doc)
