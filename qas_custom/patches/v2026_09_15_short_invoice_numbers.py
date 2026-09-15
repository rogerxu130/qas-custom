import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute():
	# Keep legacy options valid for saved drafts and historical documents.
	meta = frappe.get_meta("Sales Invoice")
	options = (meta.get_field("naming_series").options or "").splitlines()
	options = ["YY.#####", *[option for option in options if option != "YY.#####"]]
	make_property_setter("Sales Invoice", "naming_series", "options", "\n".join(options), "Text")
	make_property_setter("Sales Invoice", "naming_series", "default", "YY.#####", "Data")
	frappe.clear_cache(doctype="Sales Invoice")
