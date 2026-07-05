from __future__ import annotations

import frappe

from qas_custom.patches.v2026_06_21_migrate_class_attendance_entries import execute as migrate_attendance_entries
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE


def execute():
	if not frappe.db.table_exists(ATTENDANCE_DOCTYPE):
		return
	if not frappe.db.table_exists("Attendance Record"):
		return

	migrate_attendance_entries()
	frappe.db.delete(
		"Attendance Record",
		{
			"parenttype": "Course Sessions",
			"parentfield": "attendance_list",
		},
	)
	frappe.clear_cache(doctype="Course Sessions")
	frappe.clear_cache(doctype="Attendance Record")
