"""Global pricing must preserve purchase snapshots and skip unpriced courses."""
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.payg import pricing


class TestPaygPricing(TestCase):
    def test_four_percent_prices_match_public_course_tiers(self):
        config = {"enabled": True, "discount_percent": Decimal("4")}
        for trial, total in ((55, "528.00"), (65, "624.00"), (68, "652.80"),
                             (81, "777.60"), (85, "816.00")):
            with self.subTest(trial=trial), patch.object(pricing, "get_trial_class_fee", return_value=trial):
                self.assertEqual(pricing.card_price("C", config=config), Decimal(total))

    def test_unpriced_course_and_invalid_percent_cannot_sell(self):
        config = {"enabled": True, "discount_percent": Decimal("4")}
        with patch.object(pricing, "get_trial_class_fee", return_value=0), \
             patch.object(pricing.frappe, "throw", side_effect=lambda message: (_ for _ in ()).throw(ValueError(message))):
            with self.assertRaisesRegex(Exception, "positive trial class fee"):
                pricing.card_price("C", config=config)
            for invalid in ("-1", "100", "99.999", "nan", "hello"):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "discount"):
                    pricing.discount_percent(invalid)

    def test_configure_provisions_only_priced_courses_and_preserves_disabled_product(self):
        existing = frappe._dict(course="A", standard_card_price=400, enabled=0,
                                save=Mock())
        created = []
        singleton = frappe._dict(enabled=0, discount_percent=0, save=Mock())
        def get_doc(doctype, name=None):
            if isinstance(doctype, dict):
                created.append(doctype)
                return frappe._dict(insert=Mock())
            return existing
        with patch.object(pricing.frappe, "get_all", side_effect=[
            [frappe._dict(name="A"), frappe._dict(name="B"), frappe._dict(name="C")],
            [frappe._dict(name="OLD", course="A")]]), \
             patch.object(pricing.frappe, "get_doc", side_effect=get_doc), \
             patch.object(pricing.frappe, "get_single", return_value=singleton), \
             patch.object(pricing, "get_trial_class_fee", side_effect=lambda course: {"A": 68, "B": 55, "C": 0}[course]):
            result = pricing.configure("4")
        self.assertEqual((result["products_ready"], result["courses_without_trial_price"], result["disabled_products"]),
                         (1, ["C"], ["A"]))
        self.assertEqual(existing.standard_card_price, Decimal("652.80"))
        self.assertEqual(existing.enabled, 0)
        self.assertEqual((created[0]["course"], created[0]["standard_card_price"]), ("B", Decimal("528.00")))
        self.assertEqual((singleton.enabled, singleton.discount_percent), (1, Decimal("4.00")))
