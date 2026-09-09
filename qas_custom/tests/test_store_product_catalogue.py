from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.material_orders import _ordered_media, _product_payload, _parent_product_payload, _apply_product_categories, _product_category_names


class TestStoreProductCatalogue(TestCase):
	def setUp(self):
		translation = patch("qas_custom.services.material_orders._", side_effect=lambda text: text)
		translation.start()
		self.addCleanup(translation.stop)

	def test_media_order_is_stable_for_duplicate_and_missing_values(self):
		rows = [
			frappe._dict(label="third", display_order=2),
			frappe._dict(label="first", display_order=0),
			frappe._dict(label="second", display_order=0),
			frappe._dict(label="last"),
		]

		self.assertEqual([row.label for _, row in _ordered_media(rows)], ["first", "second", "third", "last"])

	@patch("qas_custom.services.material_orders._category_payload_for_product", return_value=None)
	def test_product_payload_returns_videos_and_images_in_explicit_order(self, _category):
		doc = SimpleNamespace(
			name="STORE-PROD-1",
			product_name="Paint kit",
			description="",
			unit_price=25,
			active=1,
			display_order=0,
			item_code="ITEM-1",
			modified="2026-08-17",
			get=lambda field: {
				"primary_category": None,
				"images": [
					frappe._dict(image="/files/second.jpg", display_order=1),
					frappe._dict(image="/files/first.jpg", display_order=0),
				],
				"videos": [
					frappe._dict(label="Second", url="https://youtu.be/two222", display_order=2),
					frappe._dict(label="First", url="https://youtu.be/one111", display_order=0),
				],
			}.get(field),
		)

		payload = _product_payload(doc, include_media=True)

		self.assertEqual([row["image"] for row in payload["images"]], ["/files/first.jpg", "/files/second.jpg"])
		self.assertEqual([row["label"] for row in payload["videos"]], ["First", "Second"])

	def category_doc(self, names=(), primary=None):
		doc = frappe._dict(name="P1", product_name="Sketchbook", unit_price=30, active=1, categories=[frappe._dict(category=name) for name in names], primary_category=primary)
		doc.set = lambda key, value: doc.update({key: value})
		return doc

	@patch("qas_custom.services.material_orders.frappe.db", new=SimpleNamespace(exists=Mock(return_value=True)))
	def test_multiple_categories_are_validated_and_deduplicated(self):
		doc = self.category_doc()
		_apply_product_categories(doc, {"category_ids": ["art", "sketching", "art"]})
		self.assertEqual(_product_category_names(doc), ["art", "sketching"])
		self.assertEqual(doc.primary_category, "art")

	def test_clearing_categories_does_not_restore_legacy_primary(self):
		doc = self.category_doc(["art", "sketching"], "art")
		_apply_product_categories(doc, {"category_ids": [], "primary_category": "art"})
		self.assertEqual(_product_category_names(doc), [])
		self.assertIsNone(doc.primary_category)

	@patch("qas_custom.services.material_orders.frappe.db", new=SimpleNamespace(exists=Mock(return_value=True)))
	def test_legacy_saves_preserve_other_memberships(self):
		doc = self.category_doc(["art", "sketching"], "art")
		_apply_product_categories(doc, {"primary_category": "supplies"})
		self.assertEqual(_product_category_names(doc), ["supplies", "sketching"])
		_apply_product_categories(doc, {})
		self.assertEqual(_product_category_names(doc), ["supplies", "sketching"])

	def test_legacy_product_falls_back_to_original_category(self):
		self.assertEqual(_product_category_names(self.category_doc(primary="art")), ["art"])

	@patch("qas_custom.services.material_orders._category_payload_for_product")
	def test_parent_sees_only_active_categories(self, category):
		category.side_effect = lambda name: {"name": name, "active": int(name != "hidden")}
		doc = self.category_doc(["hidden", "art", "sketching"], "hidden")
		self.assertEqual(len(_product_payload(doc)["categories"]), 3)
		self.assertEqual([row["name"] for row in _parent_product_payload(doc)["categories"]], ["art", "sketching"])

	@patch("qas_custom.services.material_orders.frappe.throw", side_effect=ValueError)
	def test_invalid_category_list_is_rejected(self, throw):
		for value in ("art", None, [{"name": "art"}]):
			with self.assertRaises(ValueError):
				_apply_product_categories(self.category_doc(), {"category_ids": value})

	@patch("qas_custom.services.material_orders.frappe.throw", side_effect=ValueError)
	@patch("qas_custom.services.material_orders.frappe.db", new=SimpleNamespace(exists=Mock(return_value=False)))
	def test_unknown_category_is_rejected_before_changing_memberships(self, throw):
		doc = self.category_doc(["art"], "art")
		with self.assertRaises(ValueError):
			_apply_product_categories(doc, {"category_ids": ["missing"]})
		self.assertEqual(_product_category_names(doc), ["art"])

	def test_category_migration_preserves_existing_memberships_and_is_repeatable(self):
		from qas_custom.patches.v2026_09_09_backfill_store_product_categories import execute

		legacy = self.category_doc(primary="art")
		legacy.append = lambda key, row: legacy[key].append(frappe._dict(row))
		legacy.save = Mock()
		current = self.category_doc(["art", "sketching"], "art")
		current.save = Mock()
		rows = [frappe._dict(name="legacy", primary_category="art"), frappe._dict(name="current", primary_category="art")]
		with patch("frappe.get_all", return_value=rows), patch("frappe.get_doc", side_effect=lambda doctype, name: legacy if name == "legacy" else current):
			execute()
			execute()
		self.assertEqual(_product_category_names(legacy), ["art"])
		self.assertEqual(_product_category_names(current), ["art", "sketching"])
		legacy.save.assert_called_once()
		current.save.assert_not_called()
