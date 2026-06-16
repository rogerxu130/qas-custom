import frappe


def execute():
	field_name = frappe.db.get_value("DocField", {"parent": "Student", "fieldname": "date_of_birth"}, "name")
	if field_name:
		frappe.db.set_value("DocField", field_name, "reqd", 0)
		frappe.clear_cache(doctype="Student")
