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
