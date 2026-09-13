from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services import material_orders as service


class TestStoreCategoryDelete(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.db = SimpleNamespace(get_value=Mock(return_value='v1'), exists=Mock(return_value=False))
        stack.enter_context(patch.object(frappe, 'db', self.db))
        stack.enter_context(patch.object(frappe, 'throw', side_effect=ValueError))
        stack.enter_context(patch.object(service, '_', lambda s: s))
        self.admin = stack.enter_context(patch.object(service, '_require_school_admin'))
        self.delete = stack.enter_context(patch.object(frappe, 'delete_doc'))

    def test_unused_category_deleted_only(self):
        self.assertEqual(service.delete_school_admin_store_product_category_data('C1', 'v1'), {'deleted': 'C1'})
        self.delete.assert_called_once_with('Store Product Category', 'C1', ignore_permissions=True)
        self.db.get_value.assert_called_once_with('Store Product Category', 'C1', 'modified', for_update=True)

    def test_both_modern_and_legacy_product_links_block(self):
        for linked_type in ('Store Product Category Link', 'Store Product'):
            self.db.exists.side_effect = lambda dt, filters: dt == linked_type
            with self.assertRaises(ValueError):
                service.delete_school_admin_store_product_category_data('C1', 'v1')
        self.delete.assert_not_called()

    def test_missing_stale_and_missing_version_rejected(self):
        for category, version, stored in ((None, 'v1', 'v1'), ('C1', 'old', 'v1'), ('C1', None, 'v1'), ('C1', 'v1', None)):
            self.db.get_value.return_value = stored
            with self.assertRaises(ValueError):
                service.delete_school_admin_store_product_category_data(category, version)
        self.delete.assert_not_called()

    def test_permission_checked_before_database(self):
        self.admin.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            service.delete_school_admin_store_product_category_data('C1', 'v1')
        self.db.get_value.assert_not_called()
        self.delete.assert_not_called()

    def test_assignment_locks_category_and_rejects_deleted_category(self):
        self.assertEqual(service._valid_product_category('C1'), 'C1')
        self.db.get_value.assert_called_once_with('Store Product Category', 'C1', 'name', for_update=True)
        self.db.get_value.return_value = None
        with self.assertRaises(ValueError):
            service._valid_product_category('C1')
