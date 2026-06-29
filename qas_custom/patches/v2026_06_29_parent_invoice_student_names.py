from __future__ import annotations

import frappe
from frappe.utils import flt

from qas_custom.patches.v2026_06_28_parent_invoice_format import (
	PRINT_FORMAT_NAME,
	_parent_invoice_print_html,
)


def execute():
	_add_student_display_name_field()
	_backfill_invoice_item_student_names()
	_refresh_parent_invoice_print_format()
	frappe.clear_cache(doctype="Sales Invoice Item")
	frappe.clear_cache(doctype="Sales Invoice")


def _add_student_display_name_field():
	if not frappe.db.exists("DocType", "Sales Invoice Item"):
		return
	if frappe.db.exists("DocField", {"parent": "Sales Invoice Item", "fieldname": "student_display_name"}):
		return
	if frappe.db.exists("Custom Field", {"dt": "Sales Invoice Item", "fieldname": "student_display_name"}):
		return
	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Sales Invoice Item",
			"fieldname": "student_display_name",
			"fieldtype": "Data",
			"label": "Student Name",
			"insert_after": "student",
			"read_only": 1,
		}
	).insert(ignore_permissions=True)


def _backfill_invoice_item_student_names():
	if not frappe.db.exists("DocType", "Sales Invoice Item"):
		return
	fields = ["name", "student", "description", "item_name", "item_code", "qty", "rate"]
	for fieldname in ("student_code", "student_display_name", "course", "term", "session_count"):
		if frappe.db.has_column("Sales Invoice Item", fieldname):
			fields.append(fieldname)
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"student": ["is", "set"]},
		fields=fields,
		limit_page_length=0,
	)
	for row in rows:
		student_name = _get_student_name(row.get("student"))
		if not student_name:
			continue
		updates = {"student_display_name": student_name}
		description = _updated_parent_description(row, student_name)
		if description:
			updates["description"] = description
		frappe.db.set_value("Sales Invoice Item", row.name, updates, update_modified=False)


def _updated_parent_description(row, student_name):
	description = row.get("description") or ""
	student_code = row.get("student_code")
	student_docname = row.get("student")
	old_prefixes = [prefix for prefix in (student_code, student_docname) if prefix]
	if not any(description.startswith(f"{prefix} - ") for prefix in old_prefixes):
		return None

	course = row.get("course") or row.get("item_name") or row.get("item_code") or "Course"
	term = row.get("term")
	session_count = int(flt(row.get("session_count") or row.get("qty") or 0))
	parts = [course]
	if term:
		parts.append(f"({term})")
	session_label = "1 session" if session_count == 1 else f"{session_count} sessions"
	return f"{student_name} - {' '.join(parts)}, {session_label}"


def _get_student_name(student):
	if not student or not frappe.db.exists("Student", student):
		return student
	return frappe.db.get_value("Student", student, "student_name") or student


def _refresh_parent_invoice_print_format():
	if not frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		return
	frappe.db.set_value(
		"Print Format",
		PRINT_FORMAT_NAME,
		"html",
		_parent_invoice_print_html(),
		update_modified=False,
	)
