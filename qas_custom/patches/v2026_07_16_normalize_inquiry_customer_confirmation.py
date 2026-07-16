from __future__ import annotations

import frappe


def execute():
	if not frappe.db.table_exists("Inquiry") or not frappe.db.has_column("Inquiry", "confirmation_status"):
		return

	frappe.db.sql(
		"""
		update `tabInquiry`
		set confirmation_status = 'Pending'
		where inquiry_type = 'Trial Lesson'
		  and status in ('Booked', 'Rescheduled')
		  and coalesce(confirmation_status, '') != 'Customer Confirmed'
		"""
	)
