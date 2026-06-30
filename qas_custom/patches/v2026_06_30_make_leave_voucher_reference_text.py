import frappe


def execute():
	if not frappe.db.exists("DocType", "Leave Request"):
		return
	field = frappe.db.get_value(
		"DocField",
		{"parent": "Leave Request", "fieldname": "makeup_voucher"},
		"name",
	)
	if not field:
		return
	frappe.db.set_value(
		"DocField",
		field,
		{
			"fieldtype": "Data",
			"options": "",
			"read_only": 1,
		},
		update_modified=False,
	)
	frappe.clear_cache(doctype="Leave Request")
