from __future__ import annotations

import frappe


REFERENCE_FIELDS = [
	("Inquiry", "trial_invoice"),
	("Inquiry", "converted_enrollment"),
	("Inquiry", "converted_invoice"),
	("Enrollment", "invoice"),
	("Class Attendance Entry", "source_document"),
	("Inquiry Note", "source_document"),
]


def execute():
	for doctype, fieldname in REFERENCE_FIELDS:
		_relax_reference_field(doctype, fieldname)
	frappe.clear_cache()


def _relax_reference_field(doctype, fieldname):
	for dt in ("DocField", "Custom Field"):
		filters = {"parent": doctype, "fieldname": fieldname} if dt == "DocField" else {"dt": doctype, "fieldname": fieldname}
		name = frappe.db.exists(dt, filters)
		if not name:
			continue
		frappe.db.set_value(
			dt,
			name,
			{
				"fieldtype": "Data",
				"options": None,
			},
			update_modified=False,
		)
	frappe.clear_cache(doctype=doctype)
