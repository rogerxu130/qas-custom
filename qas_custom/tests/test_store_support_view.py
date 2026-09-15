from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import material_orders as service


class TestStoreSupportView(TestCase):
    def setUp(self):
        self.parent = SimpleNamespace(name='preview-parent')
        self.frappe = SimpleNamespace(
            session=SimpleNamespace(user='admin@example.com'),
            db=Mock(), get_cached_doc=Mock(), get_all=Mock(return_value=[]),
            throw=Mock(side_effect=PermissionError), PermissionError=PermissionError,
        )
        for target, replacement in [('frappe', self.frappe), ('_', lambda s: s)]:
            patcher = patch.object(service, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_preview_parent_takes_priority_over_logged_in_account(self):
        with patch.object(service, 'get_support_view_parent', return_value=self.parent):
            self.assertIs(service._require_parent_shop(), self.parent)
        self.frappe.db.get_value.assert_not_called()

    def test_invalid_preview_never_falls_back_to_logged_in_parent(self):
        with patch.object(service, 'get_support_view_parent', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                service._require_parent_shop()
        self.frappe.db.get_value.assert_not_called()

    def test_regular_parent_keeps_existing_resolution(self):
        self.frappe.db.get_value.return_value = 'regular-parent'
        with patch.object(service, 'get_support_view_parent', return_value=None):
            service._require_parent_shop()
        self.frappe.get_cached_doc.assert_called_once_with('Parent', 'regular-parent')

    def test_preview_orders_are_scoped_to_target_parent(self):
        with patch.object(service, 'get_support_view_parent', return_value=self.parent):
            self.assertEqual(service.get_parent_store_orders_data(), {'items': []})
        self.assertEqual(self.frappe.get_all.call_args.kwargs['filters'], {'parent': self.parent.name})

    def test_foreign_order_is_denied(self):
        with patch.object(service, 'get_support_view_parent', return_value=self.parent), patch.object(service, '_get_order', return_value=SimpleNamespace(parent='another-parent')), patch.object(service, '_order_payload') as serialize:
            with self.assertRaises(PermissionError):
                service.get_parent_store_order_data('foreign-order')
        serialize.assert_not_called()

    def test_preview_order_creation_rejected_before_any_write(self):
        with patch.object(service, 'reject_support_view_write', side_effect=PermissionError), patch.object(service, '_require_parent_shop') as resolve, patch.object(service, '_create_store_order') as create:
            with self.assertRaises(PermissionError):
                service.create_parent_store_order_data({})
        resolve.assert_not_called()
        create.assert_not_called()

    def test_regular_parent_can_still_order(self):
        with patch.object(service, 'reject_support_view_write') as guard, patch.object(service, '_require_parent_shop', return_value=self.parent), patch.object(service, '_create_store_order', return_value={'name': 'order'}) as create:
            self.assertEqual(service.create_parent_store_order_data({}), {'name': 'order'})
        guard.assert_called_once_with()
        create.assert_called_once_with(self.parent, {})
