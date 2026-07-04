import frappe


def execute():
	if frappe.db.exists("DocType", "Weekly Timeslot"):
		frappe.db.set_value("DocType", "Weekly Timeslot", "allow_import", 1, update_modified=False)
		frappe.clear_cache(doctype="Weekly Timeslot")
