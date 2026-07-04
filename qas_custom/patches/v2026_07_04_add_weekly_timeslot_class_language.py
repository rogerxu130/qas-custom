import frappe


def execute():
	if not frappe.db.table_exists("Weekly Timeslot"):
		return
	if not frappe.db.has_column("Weekly Timeslot", "class_language"):
		return
	frappe.db.sql(
		"""
		update `tabWeekly Timeslot`
		set class_language = 'English'
		where class_language is null or class_language = ''
		"""
	)
	frappe.clear_cache(doctype="Weekly Timeslot")
