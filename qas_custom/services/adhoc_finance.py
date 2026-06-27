from __future__ import annotations

import frappe
from frappe.utils import flt


BALANCE_FIELDS = ("store_credit", "credit_balance", "available_credit", "balance")


def get_customer_balance_summary(customer: str | None):
	if not customer:
		return {"customer": None, "available": None, "held": 0, "source": None}

	available = _get_customer_balance_field(customer)
	return {
		"customer": customer,
		"available": available,
		"held": 0,
		"source": "Customer field" if available is not None else None,
	}


def validate_booking_credit(booking_doc):
	amount = flt(booking_doc.fee_amount)
	if amount <= 0:
		booking_doc.payment_status = "No Charge Yet"
		return None

	available = _get_customer_balance_field(booking_doc.customer)
	if available is None:
		booking_doc.payment_status = "No Charge Yet"
		return None

	if flt(available) < amount:
		frappe.throw("Insufficient balance for this Pay-as-you-go booking.")

	booking_doc.payment_status = "No Charge Yet"
	return None


def _get_customer_balance_field(customer: str | None):
	if not customer:
		return None

	for fieldname in BALANCE_FIELDS:
		if frappe.db.has_column("Customer", fieldname):
			value = frappe.db.get_value("Customer", customer, fieldname)
			if value is not None:
				return _to_float(value)
	return None


def _to_float(value):
	return flt(value)
