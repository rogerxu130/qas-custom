from __future__ import annotations

import frappe


def execute():
	if not frappe.db.table_exists("Enrollment"):
		return
	if not frappe.db.has_column("Enrollment", "pay_as_you_go_date"):
		return
	if not frappe.db.has_column("Enrollment", "enrollment_date"):
		return

	frappe.db.sql(
		"""
		update `tabEnrollment`
		set enrollment_date = pay_as_you_go_date
		where enrollment_type = 'Pay-as-you-go'
		  and coalesce(enrollment_date, '') = ''
		  and coalesce(pay_as_you_go_date, '') != ''
		"""
	)
