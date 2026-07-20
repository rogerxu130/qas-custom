from __future__ import annotations

import re

import frappe
from frappe import _


ATTENDANCE_DOCTYPE = "Class Attendance Entry"
DEFAULT_ATTENDANCE_STATUS = "To be started"


def attendance_doctype_available():
	return frappe.db.table_exists(ATTENDANCE_DOCTYPE)


def create_attendance_entry(
	course_session: str,
	student: str,
	enrollment_type: str,
	source_doctype: str | None = None,
	source_document: str | None = None,
	status: str = DEFAULT_ATTENDANCE_STATUS,
	comments: str | None = None,
	makeup_voucher: str | None = None,
	prevent_student_duplicate: bool = False,
	reactivate_cancelled_duplicate: bool = False,
):
	if not course_session:
		frappe.throw(_("Course session is required."))
	if not student:
		frappe.throw(_("Student is required before adding attendance."))
	if not enrollment_type:
		frappe.throw(_("Enrollment type is required before adding attendance."))

	if prevent_student_duplicate:
		existing_student_entry = get_student_session_attendance_entry(student, course_session)
		if existing_student_entry:
			if (
				reactivate_cancelled_duplicate
				and existing_student_entry.get("status") == "Cancelled"
				and existing_student_entry.get("enrollment_type") == enrollment_type
			):
				return reactivate_cancelled_attendance_entry(
					existing_student_entry.get("name"),
					status=status,
					comments=comments,
					source_doctype=source_doctype,
					source_document=source_document,
				)
			frappe.throw(_("This student is already listed for this session."))

	existing = (
		get_attendance_entry_by_source(source_doctype, source_document, course_session=course_session)
		if source_doctype and source_document
		else None
	)
	if existing:
		return existing

	existing_same_type = frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		{
			"course_session": course_session,
			"student": student,
			"enrollment_type": enrollment_type,
			"source_doctype": source_doctype,
			"source_document": source_document,
		},
		"name",
	)
	if existing_same_type:
		return existing_same_type

	doc = frappe.new_doc(ATTENDANCE_DOCTYPE)
	doc.course_session = course_session
	doc.student = student
	doc.enrollment_type = enrollment_type
	doc.status = status or DEFAULT_ATTENDANCE_STATUS
	doc.comments = comments
	if doc.meta.has_field("makeup_voucher"):
		doc.makeup_voucher = makeup_voucher
	if source_doctype:
		doc.source_doctype = source_doctype
	if source_doctype and source_document:
		doc.source_document = source_document
	doc.insert(ignore_permissions=True)
	return doc.name


def remove_attendance_entries_by_source(source_doctype: str, source_document: str):
	if not source_doctype or not source_document:
		return 0
	rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={"source_doctype": source_doctype, "source_document": source_document},
		pluck="name",
	)
	for name in rows:
		frappe.delete_doc(ATTENDANCE_DOCTYPE, name, ignore_permissions=True)
	return len(rows)


def cancel_attendance_entries_by_source(source_doctype: str, source_document: str):
	if not source_doctype or not source_document:
		return 0
	rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={
			"source_doctype": source_doctype,
			"source_document": source_document,
			"status": ["!=", "Cancelled"],
		},
		pluck="name",
	)
	for name in rows:
		frappe.db.set_value(ATTENDANCE_DOCTYPE, name, "status", "Cancelled", update_modified=True)
	return len(rows)


def remove_attendance_entry(attendance_entry: str | None):
	if not attendance_entry or not frappe.db.exists(ATTENDANCE_DOCTYPE, attendance_entry):
		return False
	frappe.delete_doc(ATTENDANCE_DOCTYPE, attendance_entry, ignore_permissions=True)
	return True


def get_attendance_entry_by_source(source_doctype: str | None, source_document: str | None, course_session: str | None = None):
	if not source_doctype or not source_document:
		return None
	filters = {"source_doctype": source_doctype, "source_document": source_document}
	if course_session:
		filters["course_session"] = course_session
	return frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		filters,
		"name",
	)


def get_attendance_entries(session_ids: list[str], fields: list[str] | None = None, filters: dict | None = None):
	if not session_ids:
		return []
	query_filters = {"course_session": ["in", session_ids]}
	if filters:
		query_filters.update(filters)
	return frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters=query_filters,
		fields=fields or default_attendance_fields(),
		order_by="course_session asc, creation asc",
	)


def default_attendance_fields():
	return [
		"name",
		"course_session",
		"student",
		"enrollment_type",
		"status",
		"comments",
		"makeup_voucher",
		"source_doctype",
		"source_document",
		"previous_status",
		"marked_by",
		"marked_at",
		"creation",
	]


def has_student_session_conflict(student: str, course_session: str):
	return bool(get_student_session_attendance_entry(student, course_session))


def get_student_session_attendance_entry(student: str, course_session: str):
	return frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		{
			"student": student,
			"course_session": course_session,
		},
		["name", "status", "enrollment_type"],
		as_dict=True,
	)


def reactivate_cancelled_attendance_entry(
	attendance_entry: str,
	*,
	status: str | None,
	comments: str | None,
	source_doctype: str | None,
	source_document: str | None,
):
	doc = frappe.get_doc(ATTENDANCE_DOCTYPE, attendance_entry)
	if doc.get("status") != "Cancelled":
		frappe.throw(_("This student is already listed for this session."))

	if doc.meta.has_field("previous_status"):
		doc.previous_status = "Cancelled"
	doc.status = status or DEFAULT_ATTENDANCE_STATUS
	doc.comments = comments
	doc.source_doctype = source_doctype
	doc.source_document = source_document
	for fieldname in ("marked_by", "marked_at"):
		if doc.meta.has_field(fieldname):
			doc.set(fieldname, None)
	doc.save(ignore_permissions=True)
	return doc.name


def infer_source_from_legacy_row(row):
	source_doctype = row.get("source_doctype")
	source_document = row.get("source_document")
	if source_doctype and source_document:
		return source_doctype, source_document

	comments = row.get("comments") or ""
	patterns = (
		(re.compile(r"Added from Inquiry\s+([A-Za-z0-9\-]+)"), "Inquiry"),
		(re.compile(r"Added from Adhoc Booking\s+([A-Za-z0-9\-]+)"), "Adhoc Booking"),
		(re.compile(r"Added from Enrollment\s+([A-Za-z0-9\-]+)"), "Enrollment"),
	)
	for pattern, doctype in patterns:
		match = pattern.search(comments)
		if match and frappe.db.exists(doctype, match.group(1)):
			return doctype, match.group(1)

	return None, None
