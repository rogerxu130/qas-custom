"""Test private resource fields at the service/API boundary without cloud I/O."""
import importlib.util
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class ResourcePrivacyTests(TestCase):
    def setUp(self):
        self.frappe = ModuleType('frappe')
        self.frappe.db = Mock()
        self.frappe.get_all = Mock(return_value=[])
        self.frappe.get_doc = Mock(return_value=dict(name='R1', title='Lesson', summary='Public', status='Published', modified='v1', admin_note='Internal source'))
        self.frappe.throw = Mock(side_effect=ValueError)
        utils = ModuleType('frappe.utils')
        utils.cint = lambda value: int(value or 0)
        utils.get_datetime = lambda value: value
        utils.now_datetime = datetime.now
        utils.add_days = Mock()
        modules = patch.dict(sys.modules, {'frappe': self.frappe, 'frappe.utils': utils})
        modules.start()
        self.addCleanup(modules.stop)
        path = Path(__file__).parents[1] / 'services/teaching_resources.py'
        spec = importlib.util.spec_from_file_location('_resource_privacy_test', path)
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)
        self.service.require_user = Mock()
        self.service.teacher_name = Mock(return_value='TEACHER')
        self.service.teacher_access = Mock(return_value=([{'note': 'Lesson 1'}], 900))

    def test_teacher_detail_never_returns_private_fields_or_queries_tags(self):
        result = self.service.detail('R1')
        self.assertEqual(result['tags'], [])
        self.assertNotIn('admin_note', result)
        self.assertEqual(result['summary'], 'Public')
        self.assertEqual(result['assignments'], [{'note': 'Lesson 1'}])
        self.frappe.db.sql.assert_not_called()
        self.service.teacher_access.assert_called_once_with('R1', 'TEACHER')

    def test_admin_detail_returns_note_and_tags(self):
        self.frappe.db.sql.return_value = [SimpleNamespace(tag='Internal label')]
        result = self.service.detail('R1', True)
        self.assertEqual(result['admin_note'], 'Internal source')
        self.assertEqual(result['tags'], ['Internal label'])
        self.service.require_user.assert_called_once_with(True)

    def test_teacher_tag_filter_cannot_be_used_as_an_oracle(self):
        self.service.resource_access_sql = Mock(return_value="r.status='Published'")
        self.frappe.db.sql.return_value = []
        self.service.catalog({'tag': 'Secret label'})
        query, params = self.frappe.db.sql.call_args.args
        self.assertNotIn('Teaching Resource Tag', query)
        self.assertNotIn('tag', params)
        self.assertNotIn('admin_note', query)

    def test_admin_tag_filter_is_preserved(self):
        self.frappe.db.sql.return_value = []
        self.service.catalog({'tag': 'Internal label'}, admin=True)
        query, params = self.frappe.db.sql.call_args.args
        self.assertIn('Teaching Resource Tag', query)
        self.assertEqual(params['tag'], 'internal label')
        self.service.require_user.assert_called_once_with(True)

    def save(self, **extra):
        self.doc = SimpleNamespace(name='R1', status='Draft', admin_note='Existing private note', save=Mock())
        self.service.lock_resource = Mock(return_value=self.doc)
        self.service.audit = Mock()
        self.service.detail = Mock(return_value={})
        return self.service.save(dict(name='R1', modified='v1', title='Lesson', summary='Public', **extra))

    def test_admin_note_can_be_saved_and_cleared(self):
        self.save(admin_note=' New source ')
        self.assertEqual(self.doc.admin_note, 'New source')
        self.service.require_user.assert_called_once_with(True)
        self.save(admin_note='')
        self.assertEqual(self.doc.admin_note, '')

    def test_old_admin_clients_preserve_existing_note(self):
        self.save()
        self.assertEqual(self.doc.admin_note, 'Existing private note')

    def test_note_length_is_validated(self):
        with self.assertRaises(ValueError):
            self.save(admin_note='x' * 2001)
        self.doc.save.assert_not_called()

    def test_case_variants_reuse_one_existing_tag_object(self):
        self.frappe.db.get_value.return_value = 'TAG-EXISTING'
        self.frappe.get_doc.side_effect = lambda value: SimpleNamespace(insert=Mock())
        self.save(tags=['Sketching', ' sketching, ', 'SKETCHING'])
        self.frappe.db.get_value.assert_called_once_with(self.service.TAG, {'tag': 'sketching'}, 'name')
        self.frappe.get_doc.assert_called_once_with(dict(doctype=self.service.TAG_LINK, resource='R1', tag='TAG-EXISTING'))

    def test_multiple_tags_in_one_name_are_rejected_before_saving(self):
        with self.assertRaises(ValueError):
            self.save(tags=['Sketching, Animals'])
        self.doc.save.assert_not_called()

    def metadata_cleanup_fixture(self, **changes):
        self.resource = SimpleNamespace(name='R1', status='Draft', save=Mock())
        self.metadata = SimpleNamespace(**dict(dict(name='META', resource='R1', filename='._lesson.mp4', size_bytes=4096, status='Ready', replaces='', save=Mock()), **changes))
        self.service.lock_resource = Mock(return_value=self.resource)
        self.frappe.get_doc.return_value = self.metadata
        self.frappe.db.exists.return_value = False
        self.frappe.log_error = Mock()
        self.service.audit = Mock()
        self.service.detail = Mock(return_value={'files': []})
        self.service.cleanup_file = Mock()
        return dict(resource='R1', modified='v1', files=['META'], confirmation='DELETE METADATA')

    def test_cleanup_only_targets_confirmed_metadata_after_durable_state(self):
        data = self.metadata_cleanup_fixture()
        def remove(name):
            self.assertEqual(name, 'META')
            self.assertEqual(self.metadata.status, 'Retired')
            self.frappe.db.commit.assert_called_once()
        self.service.cleanup_file.side_effect = remove
        result = self.service.cleanup_metadata(data)
        self.assertEqual(result['metadata_cleanup'], {'deleted': 1, 'pending': 0})
        self.service.require_user.assert_called_once_with(True)
        self.service.lock_resource.assert_called_once_with('R1', 'v1')

    def test_real_large_foreign_and_invalid_state_files_cannot_be_cleaned(self):
        for change in [dict(filename='lesson.mp4'), dict(size_bytes=900000000), dict(resource='OTHER'), dict(status='Cancelled')]:
            with self.subTest(change=change):
                data = self.metadata_cleanup_fixture(**change)
                with self.assertRaises(ValueError):
                    self.service.cleanup_metadata(data)
                self.service.cleanup_file.assert_not_called()
                self.metadata.save.assert_not_called()
        self.frappe.db.commit.assert_not_called()

    def test_whole_selection_is_validated_before_any_file_changes(self):
        data = self.metadata_cleanup_fixture()
        real = SimpleNamespace(name='REAL', resource='R1', filename='lesson.pptx', size_bytes=4096, status='Ready')
        self.frappe.get_doc.side_effect = [self.metadata, real]
        data['files'].append('REAL')
        with self.assertRaises(ValueError):
            self.service.cleanup_metadata(data)
        self.metadata.save.assert_not_called()
        self.service.cleanup_file.assert_not_called()

    def test_cleanup_requires_confirmation_and_no_pending_replacement(self):
        data = self.metadata_cleanup_fixture()
        with self.assertRaises(ValueError):
            self.service.cleanup_metadata({**data, 'confirmation': ''})
        self.frappe.db.exists.return_value = True
        with self.assertRaises(ValueError):
            self.service.cleanup_metadata(data)
        self.service.cleanup_file.assert_not_called()

    def test_failed_cloud_cleanup_remains_retryable_and_is_not_reported_deleted(self):
        data = self.metadata_cleanup_fixture()
        self.service.cleanup_file.side_effect = RuntimeError('Storage unavailable')
        result = self.service.cleanup_metadata(data)
        self.assertEqual(result['metadata_cleanup'], {'deleted': 0, 'pending': 1})
        self.assertEqual(self.metadata.status, 'Retired')
        self.frappe.db.rollback.assert_called_once()
        self.frappe.log_error.assert_called_once()
