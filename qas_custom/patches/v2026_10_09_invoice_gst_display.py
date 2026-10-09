from qas_custom.patches.v2026_06_28_parent_invoice_format import _upsert_parent_invoice_print_format
import frappe


def execute():
    _upsert_parent_invoice_print_format()
    frappe.clear_cache(doctype="Sales Invoice")
