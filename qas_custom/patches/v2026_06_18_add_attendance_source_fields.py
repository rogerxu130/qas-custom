from __future__ import annotations

import re

import frappe


def execute():
	_add_attendance_fields()
	_add_inquiry_note_fields()
	_add_notification_log_fields()
	_backfill_attendance_sources()
	frappe.clear_cache()


def _add_attendance_fields():
	if not frappe.db.exists("DocType", "Attendance Record"):
		return

	_ensure_custom_field(
		"Attendance Record",
		{
			"fieldname": "source_doctype",
			"fieldtype": "Link",
			"label": "Source Doctype",
			"options": "DocType",
			"insert_after": _existing_field("Attendance Record", ["enrollment_type", "status", "student"]),
			"read_only": 1,
			"description": "Business document type that created or owns this attendance row.",
		},
	)
	_ensure_custom_field(
		"Attendance Record",
		{
			"fieldname": "source_document",
			"fieldtype": "Dynamic Link",
			"label": "Source Document",
			"options": "source_doctype",
			"insert_after": "source_doctype",
			"read_only": 1,
			"description": "Business document that created or owns this attendance row.",
		},
	)
	_ensure_custom_field(
		"Attendance Record",
		{
			"fieldname": "previous_status",
			"fieldtype": "Data",
			"label": "Previous Status",
			"insert_after": _existing_field("Attendance Record", ["status", "source_document"]),
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Attendance Record",
		{
			"fieldname": "marked_by",
			"fieldtype": "Link",
			"label": "Marked By",
			"options": "User",
			"insert_after": "previous_status",
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Attendance Record",
		{
			"fieldname": "marked_at",
			"fieldtype": "Datetime",
			"label": "Marked At",
			"insert_after": "marked_by",
			"read_only": 1,
		},
	)
	frappe.clear_cache(doctype="Attendance Record")


def _add_inquiry_note_fields():
	if not frappe.db.exists("DocType", "Inquiry Note"):
		return

	_ensure_custom_field(
		"Inquiry Note",
		{
			"fieldname": "note_type",
			"fieldtype": "Select",
			"label": "Note Type",
			"options": "Manual\nSystem",
			"default": "Manual",
			"insert_after": "note",
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Inquiry Note",
		{
			"fieldname": "source_doctype",
			"fieldtype": "Link",
			"label": "Source Doctype",
			"options": "DocType",
			"insert_after": "note_type",
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Inquiry Note",
		{
			"fieldname": "source_document",
			"fieldtype": "Dynamic Link",
			"label": "Source Document",
			"options": "source_doctype",
			"insert_after": "source_doctype",
			"read_only": 1,
		},
	)
	frappe.clear_cache(doctype="Inquiry Note")


def _add_notification_log_fields():
	if not frappe.db.exists("DocType", "Notification Log"):
		return

	_ensure_custom_field(
		"Notification Log",
		{
			"fieldname": "event_key",
			"fieldtype": "Data",
			"label": "Event Key",
			"insert_after": "link",
			"read_only": 1,
			"unique": 1,
			"description": "QAS idempotency key for business-event notifications.",
		},
	)
	frappe.clear_cache(doctype="Notification Log")


def _backfill_attendance_sources():
	if not frappe.db.table_exists("Attendance Record"):
		return
	if not frappe.db.has_column("Attendance Record", "source_doctype"):
		return
	if not frappe.db.has_column("Attendance Record", "source_document"):
		return

	_backfill_from_inquiries()
	_backfill_from_adhoc_bookings()
	_backfill_from_comments()


def _backfill_from_inquiries():
	if not frappe.db.table_exists("Inquiry"):
		return
	if not frappe.db.has_column("Inquiry", "attendance_row_id"):
		return

	rows = frappe.get_all(
		"Inquiry",
		filters={"attendance_row_id": ["is", "set"]},
		fields=["name", "attendance_row_id"],
	)
	for row in rows:
		if row.attendance_row_id:
			_set_attendance_source(row.attendance_row_id, "Inquiry", row.name)


def _backfill_from_adhoc_bookings():
	if not frappe.db.table_exists("Adhoc Booking"):
		return
	if not frappe.db.has_column("Adhoc Booking", "attendance_row_id"):
		return

	rows = frappe.get_all(
		"Adhoc Booking",
		filters={"attendance_row_id": ["is", "set"]},
		fields=["name", "attendance_row_id"],
	)
	for row in rows:
		if row.attendance_row_id:
			_set_attendance_source(row.attendance_row_id, "Adhoc Booking", row.name)


def _backfill_from_comments():
	rows = frappe.get_all(
		"Attendance Record",
		fields=["name", "comments", "source_document"],
	)
	patterns = (
		(re.compile(r"Added from Enrollment\s+([A-Za-z0-9\-]+)"), "Enrollment"),
		(re.compile(r"Added from Inquiry\s+([A-Za-z0-9\-]+)"), "Inquiry"),
		(re.compile(r"Added from Adhoc Booking\s+([A-Za-z0-9\-]+)"), "Adhoc Booking"),
	)
	for row in rows:
		if row.source_document:
			continue
		comments = row.comments or ""
		for pattern, doctype in patterns:
			match = pattern.search(comments)
			if match:
				_set_attendance_source(row.name, doctype, match.group(1))
				break


def _set_attendance_source(attendance_row_id, source_doctype, source_document):
	if not attendance_row_id or not source_document:
		return
	frappe.db.set_value(
		"Attendance Record",
		attendance_row_id,
		{
			"source_doctype": source_doctype,
			"source_document": source_document,
		},
		update_modified=False,
	)


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
		return
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return

	doc = frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values})
	doc.insert(ignore_permissions=True)


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
