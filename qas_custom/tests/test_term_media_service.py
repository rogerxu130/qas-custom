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

    def test_shared_url_selects_each_posts_attachment_and_counts_storage_once(self):
        from qas_custom.services.term_media_files import write_parts
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from zipfile import ZipFile
        url = '/private/files/shared.jpg'
        files = {post: SimpleNamespace(name='F' + post, file_name='shared.jpg', file_size=3,
                 modified='unchanged', content_hash='hash', file_url=url,
                 attached_to_doctype='Session Photo Post', attached_to_name=post)
                 for post in ('P1', 'P2')}
        def records(doctype, filters=None, fields=None):
            if doctype == 'Weekly Timeslot':
                return [SimpleNamespace(name='W1', course='C1', campus='Campus', day_of_week='Saturday', start_time='10:40')]
            if doctype == 'Course Sessions':
                return [dict_session]
            if doctype == 'Session Photo Post':
                return [SimpleNamespace(name=p, title='Photos', teacher='Teacher', posted_at='', caption='') for p in files]
            if doctype == 'Session Photo Item':
                return [SimpleNamespace(name='CH' + filters['parent'], idx=1, image=url)]
            return []
        class Session(dict):
            __getattr__ = dict.__getitem__
        dict_session = Session(name='S1', session_date='2026-08-22', photos=None)
        def lookup(doctype, filters, fields=None, **kwargs):
            if doctype == 'Course': return 'Course'
            if doctype == 'File':
                # The URL-only lookup selects P2, reproducing the original bug.
                return files.get(filters.get('attached_to_name'), files['P2'])
            if doctype == 'Session Photo Post': return SimpleNamespace(course_session='S1', status='Published')
            if doctype == 'Course Sessions': return 'W1'
            if doctype == 'Weekly Timeslot': return 'T1'
            if doctype == 'Session Photo Item': return SimpleNamespace(parent=filters[2:], idx=1, image=url)
        self.mock.db.get_value.side_effect = lookup
        self.mock.db.count.return_value = 0
        self.mock.db.exists.return_value = True
        self.mock.get_doc.side_effect = lambda doctype, name: files[name[1:]]
        with patch.object(service, '_all', side_effect=records):
            rows, notes, stats = service._inventory('T1')
        self.assertEqual([r['file_id'] for r in rows], ['FP1', 'FP2'])
        self.assertEqual(stats['unique_files'], 1)
        self.assertEqual(stats['bytes'], 3)
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jpg'
            source.write_bytes(b'abc')
            with patch.object(service, '_local_path', return_value=source):
                parts = write_parts(Path(directory) / 'archive', rows, service._source)
            self.assertEqual([r['status'] for r in rows], ['exported', 'exported'])
            with ZipFile(Path(directory) / 'archive' / parts[0]['filename']) as archive:
                self.assertEqual([archive.read(r['archive_path']) for r in rows], [b'abc', b'abc'])

    def test_wrong_attachment_still_rejected(self):
        row = dict(self.row, file_modified='unchanged')
        self.mock.db.exists.return_value = True
        self.mock.get_doc.return_value = SimpleNamespace(file_url=row['url'], modified='unchanged',
            attached_to_doctype='Session Photo Post', attached_to_name='OTHER')
        with self.assertRaisesRegex(ValueError, 'file_attachment_changed'):
            service._source(row)

    def test_payload_exposes_failure_details_without_private_source_fields(self):
        import json
        doc = SimpleNamespace(name='A1', term='T1', media_type='all', status='Partial', progress=2,
            total=2, expires_at=None, backup_confirmed_at=None, cleanup_status='', error='',
            job_data=json.dumps({'entries': [dict(self.row, status='failed', filename='photo.jpg',
                reason='file_attachment_changed'), dict(self.row, status='exported')]}))
        failures = service.payload(doc)['failures']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['reason'], 'file_attachment_changed')
        self.assertEqual(failures[0]['post'], 'P1')
        self.assertNotIn('url', failures[0])
        self.assertNotIn('file_id', failures[0])
