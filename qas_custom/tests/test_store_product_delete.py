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

    def test_unused_product_uses_frappe_attachment_deletion_lifecycle(self):
        self.assertEqual(service.delete_school_admin_store_product_data("P1", "version1"), {"deleted": "P1"})
        self.admin.assert_called_once()
        self.db.get_value.assert_called_once_with("Store Product", "P1", "name", for_update=True)
        self.delete.assert_called_once_with("Store Product", "P1", ignore_permissions=True)
        self.db.set_value.assert_not_called()

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

    def test_only_shared_media_detached_before_frappe_deletes_attachments(self):
        files = [frappe._dict(name="image", file_url="/files/image.png"), frappe._dict(name="video", file_url="/files/video.mp4"), frappe._dict(name="owned", file_url="/files/owned.png")]
        def exists(doctype, filters):
            return ((doctype == "Store Product Image" and "/files/image.png" in filters["image"][1]) or
                    (doctype == "Store Product Video" and "/files/video.mp4" in filters["url"][1]))
        self.db.exists.side_effect = exists
        with patch.object(frappe, "get_all", return_value=files), patch("frappe.utils.get_url", side_effect=lambda path: "https://system.example" + path):
            service.preserve_shared_product_files("P1")
        self.assertEqual([call.args[1] for call in self.db.set_value.call_args_list], ["image", "video"])
        for call in self.db.set_value.call_args_list:
            self.assertIsNone(call.args[2]["attached_to_name"])

    def test_description_reference_preserves_file(self):
        self.db.exists.side_effect = lambda doctype, filters: doctype == "Store Product"
        with patch.object(frappe, "get_all", return_value=[frappe._dict(name="F1", file_url="/files/description.png")]), patch("frappe.utils.get_url", side_effect=lambda path: path):
            service.preserve_shared_product_files("P1")
        self.assertEqual(self.db.set_value.call_args.args[1], "F1")

    def test_product_trash_hook_preserves_shared_media(self):
        with patch.object(service, "preserve_shared_product_files") as preserve:
            StoreProduct.on_trash(self.doc)
        preserve.assert_called_once_with("P1")
