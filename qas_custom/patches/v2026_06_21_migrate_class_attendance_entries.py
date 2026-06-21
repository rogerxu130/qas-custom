from __future__ import annotations

import frappe

from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, infer_source_from_legacy_row


def execute():
	if not frappe.db.table_exists(ATTENDANCE_DOCTYPE):
		return
	if not frappe.db.table_exists("Attendance Record"):
		return

	fields = [
		"name",
		"parent",
		"student",
		"enrollment_type",
		"status",
		"comments",
		"makeup_voucher",
	]
	meta = frappe.get_meta("Attendance Record")
	for fieldname in ("source_doctype", "source_document", "previous_status", "marked_by", "marked_at"):
		if meta.has_field(fieldname):
			fields.append(fieldname)

	rows = frappe.get_all(
		"Attendance Record",
		filters={
			"parenttype": "Course Sessions",
			"parentfield": "attendance_list",
			"parent": ["is", "set"],
			"student": ["is", "set"],
		},
		fields=fields,
		order_by="parent asc, idx asc",
	)

	for row in rows:
		if not row.get("parent") or not row.get("student") or not row.get("enrollment_type"):
			continue
		if _already_migrated(row):
			continue
		source_doctype, source_document = infer_source_from_legacy_row(row)
		doc = frappe.new_doc(ATTENDANCE_DOCTYPE)
		doc.course_session = row.get("parent")
		doc.student = row.get("student")
		doc.enrollment_type = row.get("enrollment_type")
		doc.status = row.get("status") or "To be started"
		doc.comments = row.get("comments")
		if doc.meta.has_field("makeup_voucher"):
			doc.makeup_voucher = row.get("makeup_voucher")
		if source_doctype:
			doc.source_doctype = source_doctype
		if source_doctype and source_document and frappe.db.exists(source_doctype, source_document):
			doc.source_document = source_document
		if doc.meta.has_field("previous_status"):
			doc.previous_status = row.get("previous_status")
		if doc.meta.has_field("marked_by"):
			doc.marked_by = row.get("marked_by")
		if doc.meta.has_field("marked_at"):
			doc.marked_at = row.get("marked_at")
		doc.insert(ignore_permissions=True)

	frappe.clear_cache(doctype=ATTENDANCE_DOCTYPE)


def _already_migrated(row):
	source_doctype, source_document = infer_source_from_legacy_row(row)
	if source_doctype and source_document:
		existing = frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{
				"course_session": row.get("parent"),
				"student": row.get("student"),
				"source_doctype": source_doctype,
				"source_document": source_document,
			},
		)
		if existing:
			return True

	return bool(
		frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{
				"course_session": row.get("parent"),
				"student": row.get("student"),
				"enrollment_type": row.get("enrollment_type"),
				"comments": row.get("comments"),
			},
		)
	)
