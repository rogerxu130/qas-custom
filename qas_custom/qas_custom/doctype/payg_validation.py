"""Shared local validation for PAYG DocTypes."""
from decimal import Decimal, InvalidOperation
import frappe
from qas_custom.modules.payg.rules import as_brisbane_datetime

def nonnegative(doc, field):
    value = doc.get(field)
    if value is None or value == "":
        return
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        frappe.throw(f"{field} must be numeric")
    if not amount.is_finite() or amount < 0:
        frappe.throw(f"{field} cannot be negative")

def integer(doc, field, *, allow_negative=False):
    value = doc.get(field)
    try:
        number = Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        frappe.throw(f"{field} must be an integer")
    if not number.is_finite() or number != number.to_integral_value():
        frappe.throw(f"{field} must be an integer")
    if not allow_negative and number < 0:
        frappe.throw(f"{field} cannot be negative")
    return int(number)

def require_equal(doc, field, expected):
    if not expected or doc.get(field) != expected:
        frappe.throw(f"{field} does not match linked PAYG data")


def normalize_datetimes_for_storage(doc, *fields):
    """Persist Brisbane wall time without a timezone offset in MariaDB DATETIME."""
    for field in fields:
        value = doc.get(field)
        if value is not None and value != "":
            setattr(doc, field, as_brisbane_datetime(value).replace(tzinfo=None))
