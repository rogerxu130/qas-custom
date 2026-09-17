import frappe


def execute():
    doctype = "QAS Invoice Settings"
    if not frappe.db.get_single_value(doctype, "overdue_reminder_interval_days"):
        frappe.db.set_single_value(doctype, "overdue_reminder_interval_days", 4)
    frappe.clear_cache(doctype=doctype)
