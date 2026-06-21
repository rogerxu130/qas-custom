from __future__ import annotations

import frappe


def execute():
	for doctype in ("Inquiry History", "Inquiry Appointment"):
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, ignore_permissions=True, force=True)
			frappe.clear_cache(doctype=doctype)
	frappe.clear_cache()
