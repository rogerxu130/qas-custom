from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, create_autospec, patch

from frappe.model.base_document import BaseDocument
from frappe.model.rename_doc import rename_doc as framework_rename_doc
from qas_custom.services import school_admin as service


class FakeCourse:
    def __init__(self, name):
        self.name = name
        self.course_name = name
        self.course_name_zh = 'Old Chinese'
        self.meta = SimpleNamespace(autoname='field:course_name', has_field=lambda key: True)
        self.save = Mock(side_effect=self.sync)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def sync(self, **kwargs):
        # Exercise the same Frappe behavior that silently undid the old update.
        BaseDocument._sync_autoname_field(self)


class TestSchoolAdminCourseRename(TestCase):
    def test_rename_entrypoint_is_framework_function_with_permission_argument(self):
        from inspect import signature
        self.assertIs(service.rename_doc, framework_rename_doc)
        signature(service.rename_doc).bind("Course", "Old course", "New course", merge=False, ignore_permissions=True)

    def run_update(self, payload, rename_error=None):
        doc = FakeCourse('Old course')
        db = SimpleNamespace(commit=Mock())
        rename = create_autospec(framework_rename_doc, return_value='New course', side_effect=rename_error)
        fake = SimpleNamespace(get_doc=Mock(return_value=doc), db=db)
        with ExitStack() as stack:
            stack.enter_context(patch.object(service, 'frappe', fake))
            stack.enter_context(patch.object(service, 'rename_doc', rename))
            stack.enter_context(patch.object(service, '_', side_effect=lambda s: s))
            for name in ['_require_school_admin', '_apply_course_makeup_acceptance', '_apply_course_pricing_defaults', '_apply_course_invoice_item_default', '_validate_required', '_add_comment']:
                stack.enter_context(patch.object(service, name))
            get_payload = stack.enter_context(patch.object(service, '_get_course_payload', side_effect=lambda name: {'name': name}))
            if rename_error:
                with self.assertRaises(ValueError):
                    service.update_school_admin_course_data('Old course', payload)
                db.commit.assert_not_called()
                get_payload.assert_not_called()
            else:
                result = service.update_school_admin_course_data('Old course', payload)
                self.assertEqual(result['name'], 'New course' if 'course_name' in payload else 'Old course')
                db.commit.assert_called_once()
        return doc, rename

    def test_english_name_uses_real_rename_despite_frappe_save_reset(self):
        doc, rename = self.run_update({'course_name': ' New course '})
        doc.save.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(doc.course_name, 'Old course')
        rename.assert_called_once_with('Course', 'Old course', 'New course', merge=False, ignore_permissions=True)

    def test_chinese_only_edit_does_not_rename_course(self):
        doc, rename = self.run_update({'course_name_zh': 'Updated Chinese'})
        self.assertEqual(doc.course_name_zh, 'Updated Chinese')
        rename.assert_not_called()

    def test_failed_rename_never_commits_or_reports_success(self):
        self.run_update({'course_name': 'New course'}, ValueError('Name already exists'))
