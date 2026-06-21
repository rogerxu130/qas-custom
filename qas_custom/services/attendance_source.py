from __future__ import annotations

import frappe


def set_attendance_row_source(row, source_doctype: str, source_document: str | None):
	"""Attach source metadata when the linked source document already exists."""

	if row.meta.has_field("source_doctype"):
		row.source_doctype = source_doctype
	if row.meta.has_field("source_document") and source_document and frappe.db.exists(source_doctype, source_document):
		row.source_document = source_document
