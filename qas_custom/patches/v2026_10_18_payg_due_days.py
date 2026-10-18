"""Initialize the PAYG deadline without rewriting existing invoice dates."""
import frappe


def execute():
    value = frappe.db.get_single_value("QAS Invoice Settings", "payg_due_days")
    if value is None or value == "":
        frappe.db.set_single_value("QAS Invoice Settings", "payg_due_days", 7)
