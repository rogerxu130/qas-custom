import frappe


def execute():
	if not frappe.db.table_exists("Weekly Timeslot"):
		return
	if "status" not in frappe.db.get_table_columns("Weekly Timeslot"):
		return
	frappe.db.sql(
		"""
		update `tabWeekly Timeslot`
		set status = 'Active'
		where status is null or status = ''
		"""
	)
