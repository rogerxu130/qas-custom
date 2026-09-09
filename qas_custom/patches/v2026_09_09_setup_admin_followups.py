import frappe


def execute():
    doc = frappe.get_single("Admin Followup Settings")
    doc.enabled = 1
    doc.recipient = "queenslandartschool@gmail.com"
    doc.portal_url = "https://portal.queenslandartschool.com"
    doc.save(ignore_permissions=True)
