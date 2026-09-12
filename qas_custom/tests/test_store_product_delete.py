from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import material_orders as service
from qas_custom.qas_custom.doctype.store_product.store_product import StoreProduct


class TestStoreProductDelete(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = SimpleNamespace(get_value=Mock(return_value="P1"), exists=Mock(return_value=False), set_value=Mock())
        self.stack.enter_context(patch.object(frappe, "db", self.db))
        self.stack.enter_context(patch.object(frappe, "throw", side_effect=ValueError))
        self.stack.enter_context(patch.object(service, "_", lambda text: text))
        self.stack.enter_context(patch("qas_custom.qas_custom.doctype.store_product.store_product._", lambda text: text))
        self.admin = self.stack.enter_context(patch.object(service, "_require_school_admin"))
        self.delete = self.stack.enter_context(patch.object(frappe, "delete_doc"))
        self.doc = frappe._dict(name="P1", modified="version1", active=1, product_name="Kit", unit_price=25)
        self.stack.enter_context(patch.object(service, "_get_product", return_value=self.doc))

    def test_unused_product_deleted_but_files_and_item_not_deleted(self):
        self.assertEqual(service.delete_school_admin_store_product_data("P1", "version1"), {"deleted": "P1"})
        self.admin.assert_called_once()
        self.db.get_value.assert_called_once_with("Store Product", "P1", "name", for_update=True)
        self.delete.assert_called_once_with("Store Product", "P1", ignore_permissions=True)
        self.assertEqual(self.db.set_value.call_args.args[0], "File")
        self.assertIsNone(self.db.set_value.call_args.args[2]["attached_to_name"])

    def test_all_order_history_blocks_deletion(self):
        self.db.exists.return_value = True
        with self.assertRaises(ValueError):
            service.delete_school_admin_store_product_data("P1", "version1")
        self.db.exists.assert_called_once_with("Store Order Item", {"store_product": "P1"})
        self.delete.assert_not_called()
        self.db.set_value.assert_not_called()

    def test_missing_or_stale_version_rejected(self):
        for version in (None, "old-version"):
            with self.assertRaises(ValueError):
                service.delete_school_admin_store_product_data("P1", version)
        self.delete.assert_not_called()

    def test_permission_required_before_any_mutation(self):
        self.admin.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            service.delete_school_admin_store_product_data("P1", "version1")
        self.db.get_value.assert_not_called()
        self.delete.assert_not_called()

    def test_deleted_product_cannot_be_used_by_racing_order(self):
        self.db.get_value.return_value = None
        with self.assertRaises(ValueError):
            service._store_order_items([{"store_product": "P1", "qty": 1}])

    def test_order_locks_products_in_stable_order(self):
        service._store_order_items([{"store_product": "Z", "qty": 1}, {"store_product": "A", "qty": 1}])
        self.assertEqual([call.args[1] for call in self.db.get_value.call_args_list], ["A", "Z"])
        self.assertTrue(all(call.kwargs["for_update"] for call in self.db.get_value.call_args_list))

    def test_desk_deletion_also_blocks_used_product(self):
        self.db.exists.return_value = True
        with self.assertRaises(ValueError):
            StoreProduct.on_trash(self.doc)
