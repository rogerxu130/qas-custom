import frappe


def execute():
    frappe.reload_doc('qas_custom', 'doctype', 'teaching_resource')
