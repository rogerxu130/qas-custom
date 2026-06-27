from __future__ import annotations

import frappe


def execute():
	_normalize_adhoc_booking_payment_status()
	_normalize_adhoc_booking_history()
	if frappe.db.exists("DocType", "Adhoc Balance Hold"):
		frappe.delete_doc("DocType", "Adhoc Balance Hold", ignore_permissions=True, force=True)
		frappe.clear_cache(doctype="Adhoc Balance Hold")
	frappe.clear_cache()


def _normalize_adhoc_booking_payment_status():
	if not frappe.db.table_exists("Adhoc Booking"):
		return
	if not frappe.db.has_column("Adhoc Booking", "payment_status"):
		return

	frappe.db.sql(
		"""
		update `tabAdhoc Booking`
		set payment_status = 'Manual Review'
		where payment_status = 'Held'
		"""
	)
	frappe.db.sql(
		"""
		update `tabAdhoc Booking`
		set payment_status = 'No Charge Yet'
		where payment_status = 'Released'
		"""
	)


def _normalize_adhoc_booking_history():
	if not frappe.db.table_exists("Adhoc Booking History"):
		return
	if not frappe.db.has_column("Adhoc Booking History", "event_type"):
		return

	frappe.db.sql(
		"""
		update `tabAdhoc Booking History`
		set event_type = 'manual_adjustment'
		where event_type in ('balance_held', 'balance_released', 'balance_charged')
		"""
	)
