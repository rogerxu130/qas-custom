import frappe


def execute():
	if not frappe.db.exists("DocType", "QAS Invoice Settings"):
		return
	if not frappe.db.has_column("QAS Invoice Settings", "school_logo"):
		frappe.reload_doc("qas_custom", "doctype", "qas_invoice_settings")
	frappe.clear_cache(doctype="QAS Invoice Settings")
