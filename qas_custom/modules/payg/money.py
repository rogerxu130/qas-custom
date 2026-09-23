"""PAYG Currency values as stored by Frappe's DECIMAL(21,9) columns."""
from decimal import Decimal, ROUND_HALF_UP


PAYG_CURRENCY_QUANTUM = Decimal("0.000000001")


def stored_currency(value):
    """Round half away from zero to the database's nine fractional digits."""
    return Decimal(str(value)).quantize(PAYG_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
