from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.services.material_orders import _ordered_media, _product_payload


class TestStoreProductCatalogue(TestCase):
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
