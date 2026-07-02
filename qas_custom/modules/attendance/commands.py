from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, now_datetime

from qas_custom.modules.common import set_if_field
from qas_custom.modules.inquiry.commands import sync_trial_inquiry_from_attendance
from qas_custom.modules.inquiry.notes import add_attendance_sync_note
from qas_custom.services.class_attendance import cancel_attendance_entries_by_source
from qas_custom.services.class_attendance import create_attendance_entry, get_attendance_entry_by_source
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.class_attendance import remove_attendance_entries_by_source
from qas_custom.utils.environment import sendmail_or_skip


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

	notify_parent_if_present(row, previous_status, status, actor)
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


def notify_parent_if_present(row, previous_status, status, actor):
	if status not in PRESENT_STATUSES or previous_status in PRESENT_STATUSES:
		return

	event_key = f"attendance_present:{row.name}"
	if _notification_event_exists(event_key):
		return

	context = _get_attendance_context(row)
	parent = context.get("parent")
	if not parent:
		return

	parent_info = frappe.db.get_value("Parent", parent, ["name", "linked_user"], as_dict=True) or {}
	recipient = _get_parent_email(parent_info.get("linked_user"))
	if not recipient:
		return

	subject = _("Your child is present in class")
	message = _build_present_message(context)
	log_name = _create_notification_log(
		event_key=event_key,
		for_user=parent_info.get("linked_user"),
		subject=subject,
		message=message,
		document_type="Course Sessions",
		document_name=row.course_session,
		from_user=actor,
	)

	try:
		sendmail_or_skip(
			action="attendance_present_notification",
			recipients=[recipient],
			subject=subject,
			message=message,
			delayed=False,
		)
	except Exception:
		frappe.log_error(
			title="QAS Attendance Present Notification Failed",
			message=frappe.get_traceback(),
			reference_doctype="Notification Log",
			reference_name=log_name,
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


def _get_attendance_context(row):
	session_doc = frappe.get_doc("Course Sessions", row.course_session)
	student = frappe.db.get_value("Student", row.get("student"), ["name", "student_name", "guardian"], as_dict=True) or {}
	timeslot = {}
	if session_doc.get("weekly_timeslot"):
		timeslot = frappe.db.get_value(
			"Weekly Timeslot",
			session_doc.get("weekly_timeslot"),
			["course", "campus", "teacher", "start_time", "end_time"],
			as_dict=True,
		) or {}
	teacher_name = None
	if timeslot.get("teacher"):
		teacher_meta = frappe.get_meta("Teacher")
		if teacher_meta.has_field("teacher_name"):
			teacher_name = frappe.db.get_value("Teacher", timeslot.get("teacher"), "teacher_name")
		teacher_name = teacher_name or timeslot.get("teacher")
	return {
		"student": student.get("name") or row.get("student"),
		"student_name": student.get("student_name") or row.get("student"),
		"parent": student.get("guardian"),
		"course": timeslot.get("course"),
		"campus": timeslot.get("campus"),
		"teacher": teacher_name,
		"session_date": session_doc.get("session_date"),
		"start_time": timeslot.get("start_time"),
	}


def _build_present_message(context):
	lines = [
		_("Hello,"),
		"",
		_("{0} has been marked present in class.").format(escape_html(context.get("student_name") or "")),
		"",
		_("Course: {0}").format(escape_html(context.get("course") or "-")),
		_("Date: {0}").format(escape_html(context.get("session_date") or "-")),
		_("Time: {0}").format(escape_html(context.get("start_time") or "-")),
		_("Campus: {0}").format(escape_html(context.get("campus") or "-")),
	]
	if context.get("teacher"):
		lines.append(_("Teacher: {0}").format(escape_html(context.get("teacher"))))
	lines.extend(["", _("Queensland Art School")])
	return "<br>".join(lines)


def _get_parent_email(linked_user):
	if not linked_user:
		return None
	return frappe.db.get_value("User", linked_user, "email") or linked_user


def _notification_event_exists(event_key):
	if not frappe.db.exists("DocType", "Notification Log"):
		return False
	if frappe.get_meta("Notification Log").has_field("event_key"):
		return bool(frappe.db.exists("Notification Log", {"event_key": event_key}))
	return bool(frappe.db.exists("Notification Log", {"document_name": event_key}))


def _create_notification_log(event_key, for_user, subject, message, document_type, document_name, from_user):
	if not frappe.db.exists("DocType", "Notification Log"):
		return None

	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.for_user = for_user
	log.type = "Alert"
	log.email_content = message
	log.document_type = document_type
	log.document_name = document_name
	log.from_user = from_user
	if log.meta.has_field("event_key"):
		log.event_key = event_key
	log.flags.ignore_permissions = True
	log.insert()
	return log.name
