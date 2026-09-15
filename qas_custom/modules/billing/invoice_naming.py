"""Short, year-scoped invoice identifiers; existing documents are never renamed."""
import frappe
from frappe.model.naming import make_autoname


def name_invoice(doc, method=None):
	# Preserve deliberate import names; all normal new invoices, including amendments,
	# get a new numeric identifier while keeping the amended_from audit link.
	if frappe.flags.in_import or doc.flags.name_set:
		return
	name = make_autoname("YY.#####", doc=doc)
	while frappe.db.exists("Sales Invoice", name):
		name = make_autoname("YY.#####", doc=doc)
	doc.set_new_name(set_name=name)
