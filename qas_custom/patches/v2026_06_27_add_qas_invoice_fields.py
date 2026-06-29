from __future__ import annotations

import frappe


def execute():
	_add_sales_invoice_fields()
	_add_sales_invoice_item_fields()
	frappe.clear_cache()


def _add_sales_invoice_fields():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return

	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_billing_section",
			"fieldtype": "Section Break",
			"label": "QAS Billing",
			"insert_after": _existing_field("Sales Invoice", ["customer", "customer_name", "due_date"]),
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "parent",
			"fieldtype": "Link",
			"label": "Parent",
			"options": "Parent",
			"insert_after": "qas_billing_section",
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "primary_student",
			"fieldtype": "Link",
			"label": "Primary Student",
			"options": "Student",
			"insert_after": "parent",
			"in_standard_filter": 1,
			"description": "Convenience field for list/search only. The authoritative student context is stored on each invoice item row.",
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "student_summary",
			"fieldtype": "Data",
			"label": "Student Summary",
			"insert_after": "primary_student",
			"in_list_view": 1,
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_invoice_type",
			"fieldtype": "Select",
			"label": "QAS Invoice Type",
			"options": "Course\nStore Credit Top-up\nHoliday Program\nMaterial Order\nOther",
			"default": "Course",
			"insert_after": "student_summary",
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "source_doctype",
			"fieldtype": "Link",
			"label": "Source DocType",
			"options": "DocType",
			"insert_after": "qas_invoice_type",
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "source_document",
			"fieldtype": "Data",
			"label": "Source Document",
			"insert_after": "source_doctype",
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "billing_note",
			"fieldtype": "Small Text",
			"label": "Billing Note",
			"insert_after": "source_document",
		},
	)
	frappe.clear_cache(doctype="Sales Invoice")


def _add_sales_invoice_item_fields():
	if not frappe.db.exists("DocType", "Sales Invoice Item"):
		return

	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "qas_line_section",
			"fieldtype": "Section Break",
			"label": "QAS Line Context",
			"insert_after": _existing_field("Sales Invoice Item", ["description", "rate", "amount"]),
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "qas_line_type",
			"fieldtype": "Select",
			"label": "QAS Line Type",
			"options": "Course Fee\nAdhoc Credit\nHoliday Program\nMaterial\nOther",
			"default": "Course Fee",
			"insert_after": "qas_line_section",
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "student",
			"fieldtype": "Link",
			"label": "Student",
			"options": "Student",
			"insert_after": "qas_line_type",
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "student_display_name",
			"fieldtype": "Data",
			"label": "Student Name",
			"insert_after": "student",
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "student_code",
			"fieldtype": "Data",
			"label": "Student Code",
			"insert_after": "student_display_name",
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "enrollment",
			"fieldtype": "Data",
			"label": "Enrollment",
			"insert_after": "student_code",
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "course",
			"fieldtype": "Link",
			"label": "Course",
			"options": "Course",
			"insert_after": "enrollment",
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "term",
			"fieldtype": "Link",
			"label": "Term",
			"options": "Term",
			"insert_after": "course",
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "course_session",
			"fieldtype": "Link",
			"label": "Course Session",
			"options": "Course Sessions",
			"insert_after": "term",
		},
	)
	_ensure_custom_field(
		"Sales Invoice Item",
		{
			"fieldname": "session_count",
			"fieldtype": "Int",
			"label": "Session Count",
			"insert_after": "course_session",
		},
	)
	frappe.clear_cache(doctype="Sales Invoice Item")


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return
	if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
		return

	frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values}).insert(ignore_permissions=True)


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
