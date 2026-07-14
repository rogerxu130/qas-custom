from __future__ import annotations

import frappe


LEGACY_LEAVE_REQUEST_SCRIPT = "Leave Request Auto Process"


def execute():
	"""Disable the obsolete script that still reads Course Sessions.attendance_list."""
	if not frappe.db.table_exists("Server Script"):
		return
	if not frappe.db.exists("Server Script", LEGACY_LEAVE_REQUEST_SCRIPT):
		return

	frappe.db.set_value(
		"Server Script",
		LEGACY_LEAVE_REQUEST_SCRIPT,
		"disabled",
		1,
		update_modified=False,
	)
	frappe.clear_cache()
