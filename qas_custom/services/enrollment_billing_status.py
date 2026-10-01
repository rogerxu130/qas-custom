"""Live billing labels, independent of enrollment/attendance state."""

import frappe
from frappe.utils import cint, flt


def billing_state(invoice):
	if not invoice or cint(invoice.get("docstatus")) == 2 or invoice.get("status") == "Cancelled":
		return "Not invoiced"
	if cint(invoice.get("docstatus")) == 0:
		return "Draft"
	if flt(invoice.get("outstanding_amount")) <= 0:
		return "Paid"
	if flt(invoice.get("outstanding_amount")) < flt(invoice.get("grand_total")):
		return "Partly Paid"
	return "Unpaid"


def attach_billing_status(rows):
	if not rows:
		return rows
	# Item links are authoritative for shared family invoices and amendments.
	links = frappe.get_all("Sales Invoice Item", filters={
		"enrollment": ["in", [row["name"] for row in rows]],
	}, fields=["enrollment", "parent"], limit_page_length=0)
	invoice_ids = {link.parent for link in links} | {row.get("invoice") for row in rows if row.get("invoice")}
	invoices = frappe.get_all("Sales Invoice", filters={
		"name": ["in", sorted(invoice_ids)], "docstatus": ["!=", 2], "status": ["!=", "Cancelled"],
	}, fields=["name", "docstatus", "status", "grand_total", "outstanding_amount"],
		order_by="modified desc", limit_page_length=0) if invoice_ids else []
	linked = {}
	for link in links:
		linked.setdefault(link.enrollment, set()).add(link.parent)
	for row in rows:
		candidates = linked.get(row["name"], set()) | ({row["invoice"]} if row.get("invoice") else set())
		invoice = next((item for item in invoices if item.name in candidates), None)
		row["billing_state"] = billing_state(invoice)
		row["billing_invoice"] = invoice.name if invoice else None
		row["awaiting_attendance"] = row.get("status") == "Planned" and bool(invoice)
	return rows
