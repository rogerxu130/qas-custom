"""One site-wide discount for administrator-issued ten-session cards."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import frappe

from qas_custom.modules.billing.commands import get_trial_class_fee


SETTINGS = "QAS PAYG Pricing Settings"


def discount_percent(value):
    try:
        percent = Decimal(str(value))
    except (InvalidOperation, TypeError):
        frappe.throw("PAYG discount must be a number from 0 to less than 100")
    if not percent.is_finite() or not 0 <= percent < 100:
        frappe.throw("PAYG discount must be a number from 0 to less than 100")
    percent = percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if percent >= 100:
        frappe.throw("PAYG discount must be a number from 0 to less than 100")
    return percent


def settings():
    if not getattr(frappe.local, "site", None):
        return {"enabled": False, "discount_percent": Decimal("4")}
    getter = getattr(frappe.db, "get_single_value", None)
    if getter is None:
        return {"enabled": False, "discount_percent": Decimal("4")}
    enabled = bool(int(getter(SETTINGS, "enabled") or 0))
    raw = getter(SETTINGS, "discount_percent")
    return {"enabled": enabled, "discount_percent": discount_percent(4 if raw is None else raw)}


def card_price(course, product=None, config=None):
    """Return the two-decimal card price; existing operations retain their snapshot."""
    config = config if config is not None else settings()
    if not config["enabled"]:
        return Decimal(str(product.standard_card_price or 0)) if product else Decimal("0")
    return discounted_card_price(get_trial_class_fee(course), config["discount_percent"], course)


def discounted_card_price(single_class_price, percent, course=""):
    trial = Decimal(str(single_class_price or 0))
    if trial <= 0:
        frappe.throw(f"Course {course} needs a positive trial class fee before a PAYG card can be issued")
    return (trial * 10 * (100 - Decimal(str(percent))) / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def configure(percent):
    """Enable the global rule and provision products for priced active courses."""
    percent = discount_percent(percent)
    courses = frappe.get_all("Course", filters={"status": "Active", "is_makeup_course": 0},
                             fields=["name"], order_by="name asc", limit_page_length=0)
    existing = {row.course: row.name for row in frappe.get_all(
        "QAS PAYG Product", fields=["name", "course"], limit_page_length=0)}
    ready, skipped, disabled = [], [], []
    for row in courses:
        trial = get_trial_class_fee(row.name)
        if Decimal(str(trial or 0)) <= 0:
            skipped.append(row.name)
            continue
        price = discounted_card_price(trial, percent, row.name)
        if row.name in existing:
            product = frappe.get_doc("QAS PAYG Product", existing[row.name])
            product.standard_card_price = price
            # Respect products deliberately disabled by an administrator.
            product.save(ignore_permissions=True)
            if not product.enabled:
                disabled.append(row.name)
        else:
            frappe.get_doc({"doctype": "QAS PAYG Product", "course": row.name,
                            "standard_card_price": price, "sessions_per_card": 10,
                            "enabled": 1}).insert(ignore_permissions=True)
        if row.name not in disabled:
            ready.append(row.name)
    doc = frappe.get_single(SETTINGS)
    doc.enabled = 1
    doc.discount_percent = percent
    doc.save(ignore_permissions=True)
    return {"enabled": True, "discount_percent": float(percent),
            "products_ready": len(ready), "courses_without_trial_price": skipped,
            "disabled_products": disabled}
