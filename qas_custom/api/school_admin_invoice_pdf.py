import frappe

from qas_custom.modules.notifications.commands import render_parent_invoice_pdf
from qas_custom.services.school_admin import _require_school_admin


@frappe.whitelist()
def school_admin_download_invoice_pdf(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw("Invoice is required.")
	if not frappe.db.exists("Sales Invoice", invoice):
		frappe.throw("Invoice was not found.")

	frappe.local.response.filename = f"{invoice}.pdf"
	frappe.local.response.filecontent = render_parent_invoice_pdf(invoice)
	frappe.local.response.type = "download"
