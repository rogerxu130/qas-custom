"""Enable the separate Order Invoice category without renaming historical invoices."""
import frappe
from qas_custom.patches.v2026_09_03_add_workshop_invoice_options import _add_option


def execute():
    _add_option("Sales Invoice", "qas_invoice_type", "Store Order")
    frappe.clear_cache(doctype="Sales Invoice")
