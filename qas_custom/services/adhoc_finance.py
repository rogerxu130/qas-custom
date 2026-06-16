from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import flt, now_datetime


BALANCE_FIELDS = ("store_credit", "credit_balance", "available_credit", "balance")


def get_customer_balance_summary(customer: str | None):
	if not customer:
		return {"customer": None, "available": None, "held": 0, "source": None}

	available = _get_customer_balance_field(customer)
	held = _get_held_amount(customer)
	return {
		"customer": customer,
		"available": available,
		"held": held,
		"source": "Customer field" if available is not None else None,
	}


def hold_booking_amount(booking_doc):
	amount = flt(booking_doc.fee_amount)
	if amount <= 0:
		booking_doc.payment_status = "No Charge Yet"
		booking_doc.balance_hold_amount = 0
		return None

	available = _get_customer_balance_field(booking_doc.customer)
	if available is None:
		booking_doc.payment_status = "No Charge Yet"
		booking_doc.balance_hold_amount = 0
		return None

	if available is not None and flt(available) < amount:
		frappe.throw("Insufficient balance for this Pay-as-you-go booking.")

	hold = frappe.get_doc(
		{
			"doctype": "Adhoc Balance Hold",
			"adhoc_booking": booking_doc.name,
			"customer": booking_doc.customer,
			"amount": amount,
			"status": "Held",
			"held_at": now_datetime(),
		}
	)
	hold.insert(ignore_permissions=True)
	booking_doc.payment_status = "Held"
	booking_doc.balance_hold_amount = amount
	return hold


def release_booking_hold(booking_doc):
	holds = _get_active_holds(booking_doc.name)
	for hold_name in holds:
		hold = frappe.get_doc("Adhoc Balance Hold", hold_name)
		hold.status = "Released"
		hold.released_at = now_datetime()
		hold.save(ignore_permissions=True)

	booking_doc.payment_status = "Released"
	return len(holds)


def charge_booking_hold(booking_doc):
	holds = _get_active_holds(booking_doc.name)
	for hold_name in holds:
		hold = frappe.get_doc("Adhoc Balance Hold", hold_name)
		hold.status = "Charged"
		hold.charged_at = now_datetime()
		hold.save(ignore_permissions=True)

	if holds:
		booking_doc.payment_status = "Charged"
	elif booking_doc.payment_status in ("Held", "No Charge Yet"):
		booking_doc.payment_status = "Manual Review"
	return len(holds)


def _get_customer_balance_field(customer: str | None):
	if not customer:
		return None

	for fieldname in BALANCE_FIELDS:
		if frappe.db.has_column("Customer", fieldname):
			value = frappe.db.get_value("Customer", customer, fieldname)
			if value is not None:
				return _to_float(value)
	return None


def _get_held_amount(customer: str):
	rows = frappe.get_all(
		"Adhoc Balance Hold",
		filters={"customer": customer, "status": "Held"},
		fields=["amount"],
	)
	return sum(flt(row.amount) for row in rows)


def _get_active_holds(booking: str):
	return frappe.get_all(
		"Adhoc Balance Hold",
		filters={"adhoc_booking": booking, "status": "Held"},
		pluck="name",
	)


def _to_float(value):
	if isinstance(value, Decimal):
		return float(value)
	return flt(value)
