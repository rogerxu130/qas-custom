import frappe


def execute():
    frappe.reload_doc('qas_custom', 'doctype', 'teaching_resource_file')
    frappe.db.add_index('Teaching Resource File', ['kind', 'status', 'preview_status'])
