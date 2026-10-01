from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services import teacher_directory as directory
from qas_custom.services import school_admin as admin


class TeacherEmailExportTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(directory, '_doctype_available', return_value=True))
        self.stack.enter_context(patch.object(directory, '_has_field', return_value=True))
        self.rows = self.stack.enter_context(patch.object(directory.frappe, 'get_all', return_value=[]))

    def test_active_scope_and_complete_roster(self):
        self.rows.return_value = [dict(name=f'T{i}', teacher_name=f'Teacher {i}', email=f't{i}@example.com') for i in range(601)]
        result = directory.get_teacher_email_export_data()
        self.assertEqual(result['email_count'], 601)
        self.assertEqual(self.rows.call_args.kwargs['filters'], {'status': 'Active'})
        self.assertEqual(self.rows.call_args.kwargs['limit_page_length'], 0)

    def test_all_scope_includes_inactive_and_empty_roster(self):
        result = directory.get_teacher_email_export_data('all')
        self.assertEqual(self.rows.call_args.kwargs['filters'], {})
        self.assertEqual(result['teacher_count'], 0)
        self.assertEqual(result['items'], [])

    def test_normalizes_deduplicates_and_excludes_invalid_mailboxes(self):
        self.rows.return_value = [dict(name=f'T{i}', teacher_name=f'Teacher {i}', email=email) for i, email in enumerate([
            '  Teacher@Example.com ', 'teacher@example.com', '', None, 'bad',
            'a@example.com,b@example.com', 'Name <name@example.com>', 'a@example.com\nb@example.com',
            'second+tag@example.com',
        ])]
        result = directory.get_teacher_email_export_data()
        self.assertEqual(result['items'], [
            {'teacher_name': 'Teacher 0', 'email': 'teacher@example.com'},
            {'teacher_name': 'Teacher 8', 'email': 'second+tag@example.com'},
        ])
        self.assertEqual((result['teacher_count'], result['missing_count'], result['invalid_count'], result['duplicate_count']), (9, 2, 4, 1))

    def test_invalid_scope_fails_before_query(self):
        with patch.object(directory.frappe, '_', side_effect=lambda text: text), patch.object(directory.frappe, 'throw', side_effect=ValueError):
            with self.assertRaises(ValueError):
                directory.get_teacher_email_export_data('anything')
        self.rows.assert_not_called()

    def test_missing_email_schema_does_not_look_like_empty_success(self):
        with patch.object(directory, '_has_field', return_value=False), patch.object(directory.frappe, '_', side_effect=lambda text: text), patch.object(directory.frappe, 'throw', side_effect=ValueError):
            with self.assertRaises(ValueError):
                directory.get_teacher_email_export_data()
        self.rows.assert_not_called()

    def test_school_admin_permission_checked_before_data_access(self):
        with patch.object(admin, '_require_school_admin', side_effect=PermissionError), patch.object(admin, 'get_teacher_email_export_data') as export:
            with self.assertRaises(PermissionError):
                admin.get_school_admin_teacher_email_export_data('all')
            export.assert_not_called()
        with patch.object(admin, '_require_school_admin') as require, patch.object(admin, 'get_teacher_email_export_data', return_value={'items': []}) as export:
            self.assertEqual(admin.get_school_admin_teacher_email_export_data('all'), {'items': []})
            require.assert_called_once_with()
            export.assert_called_once_with(scope='all')
