from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from qas_custom.modules.common import set_if_field
from qas_custom.modules.inquiry.commands import sync_trial_inquiry_from_attendance
from qas_custom.modules.inquiry.notes import add_attendance_sync_note
from qas_custom.services.class_attendance import cancel_attendance_entries_by_source
from qas_custom.services.class_attendance import create_attendance_entry, get_attendance_entry_by_source
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.class_attendance import remove_attendance_entries_by_source


DEFAULT_ATTENDANCE_STATUS = "To be started"
FULL_TERM = "Full-Term"
PRESENT_STATUSES = {"Present", "Late"}
ABSENT_STATUSES = {"Absent"}
TRIAL_ENROLLMENT_TYPE = "Trial"


def create_full_term_attendance_entries(sessions, student: str, enrollment: str):
	for session in sessions:
		create_attendance_entry(
			course_session=session.name,
			student=student,
			enrollment_type=FULL_TERM,
			source_doctype="Enrollment",
			source_document=enrollment,
			status=DEFAULT_ATTENDANCE_STATUS,
			comments=f"Added from Enrollment {enrollment}",
		)


def ensure_trial_inquiry_attendance_entry(inquiry_doc):
	if inquiry_doc.inquiry_type != "Trial Lesson":
		return
	if inquiry_doc.status == "Cancelled":
		return
	if not inquiry_doc.course_session or not inquiry_doc.student:
		return

	if get_attendance_entry_by_source("Inquiry", inquiry_doc.name, course_session=inquiry_doc.course_session):
		return
	create_attendance_entry(
		course_session=inquiry_doc.course_session,
		student=inquiry_doc.student,
		enrollment_type=TRIAL_ENROLLMENT_TYPE,
		source_doctype="Inquiry",
		source_document=inquiry_doc.name,
		status=DEFAULT_ATTENDANCE_STATUS,
		comments=f"Added from Inquiry {inquiry_doc.name}",
		prevent_student_duplicate=True,
		reactivate_cancelled_duplicate=True,
	)


def remove_trial_inquiry_attendance_entries(inquiry):
	return remove_attendance_entries_by_source("Inquiry", inquiry)


def cancel_trial_inquiry_attendance_entries(inquiry):
	return cancel_attendance_entries_by_source("Inquiry", inquiry)


def update_attendance_status(course_session, attendance_row, status, actor=None, comment=None, validate_access=None):
	"""Update one attendance row and run downstream business effects."""

	if not course_session:
		frappe.throw(_("Course session is required."))
	if not attendance_row:
		frappe.throw(_("Attendance row is required."))

	actor = actor or frappe.session.user
	status = (status or "").strip()
	comment = (comment or "").strip()

	_validate_status(status)
	if validate_access:
		validate_access(course_session=course_session, attendance_row=attendance_row)

	row = _get_attendance_entry(course_session, attendance_row)
	previous_status = row.get("status")

	if previous_status == status and (row.get("comments") or "") == comment:
		return {
			"attendance_row": row.name,
			"attendance_entry": row.name,
			"previous_status": previous_status,
			"status": status,
			"changed": False,
		}

	set_if_field(row, "previous_status", previous_status)
	set_if_field(row, "status", status or None)
	set_if_field(row, "comments", comment)
	set_if_field(row, "marked_by", actor)
	set_if_field(row, "marked_at", now_datetime())

	row.save(ignore_permissions=True)

	sync_trial_inquiry_status_from_attendance(row, previous_status, status, actor, comment)

	return {
		"attendance_row": row.name,
		"attendance_entry": row.name,
		"previous_status": previous_status,
		"status": status,
		"changed": previous_status != status,
	}


def sync_trial_inquiry_status_from_attendance(row, previous_status, status, actor, comment):
	inquiry = _get_row_source(row, "Inquiry")
	if not inquiry or row.get("enrollment_type") != TRIAL_ENROLLMENT_TYPE:
		return
	if status not in PRESENT_STATUSES and status not in ABSENT_STATUSES:
		return

	target_status = "Completed" if status in PRESENT_STATUSES else "No-show"
	result = sync_trial_inquiry_from_attendance(inquiry, target_status)
	if result.get("skipped"):
		return
	add_attendance_sync_note(
		inquiry_doc=result["inquiry"],
		attendance_entry=row,
		status=status,
		target_status=target_status,
		previous_status=previous_status,
		comment=comment,
		actor=actor,
	)


def _validate_status(status):
	if not status:
		return
	field = frappe.get_meta(ATTENDANCE_DOCTYPE).get_field("status")
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	if options and status not in options:
		frappe.throw(_("Invalid attendance status: {0}").format(status))


def _get_attendance_entry(course_session, attendance_entry):
	row = frappe.get_doc(ATTENDANCE_DOCTYPE, attendance_entry)
	if row.course_session != course_session:
		frappe.throw(_("Invalid attendance row."))
	return row


def _get_row_source(row, doctype):
	if row.get("source_doctype") == doctype and row.get("source_document"):
		return row.get("source_document")
	return None
