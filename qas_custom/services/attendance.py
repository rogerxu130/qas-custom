from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, now_datetime


PRESENT_STATUSES = {"Present", "Late"}
ABSENT_STATUSES = {"Absent"}
TRIAL_ENROLLMENT_TYPE = "Trial"
FINAL_INQUIRY_STATUSES = {"Converted", "Cancelled", "Inactive"}


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

	session_doc = frappe.get_doc("Course Sessions", course_session)
	row = _get_attendance_row(session_doc, attendance_row)
	previous_status = row.get("status")

	if previous_status == status and (row.get("comments") or "") == comment:
		return {
			"attendance_row": row.name,
			"previous_status": previous_status,
			"status": status,
			"changed": False,
		}

	_set_if_field(row, "previous_status", previous_status)
	_set_if_field(row, "status", status or None)
	_set_if_field(row, "comments", comment)
	_set_if_field(row, "marked_by", actor)
	_set_if_field(row, "marked_at", now_datetime())

	session_doc.save(ignore_permissions=True)

	_notify_parent_if_present(session_doc, row, previous_status, status, actor)
	_sync_trial_inquiry_from_attendance(session_doc, row, previous_status, status, actor, comment)

	return {
		"attendance_row": row.name,
		"previous_status": previous_status,
		"status": status,
		"changed": previous_status != status,
	}


def _validate_status(status):
	if not status:
		return
	field = frappe.get_meta("Attendance Record").get_field("status")
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	if options and status not in options:
		frappe.throw(_("Invalid attendance status: {0}").format(status))


def _get_attendance_row(session_doc, row_id):
	for row in session_doc.get("attendance_list", []):
		if row.name == row_id:
			return row
	frappe.throw(_("Invalid attendance row."))


def _notify_parent_if_present(session_doc, row, previous_status, status, actor):
	if status not in PRESENT_STATUSES or previous_status in PRESENT_STATUSES:
		return

	event_key = f"attendance_present:{row.name}"
	if _notification_event_exists(event_key):
		return

	context = _get_attendance_context(session_doc, row)
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
		document_name=session_doc.name,
		from_user=actor,
	)

	try:
		frappe.sendmail(recipients=[recipient], subject=subject, message=message, delayed=False)
	except Exception:
		frappe.log_error(
			title="QAS Attendance Present Notification Failed",
			message=frappe.get_traceback(),
			reference_doctype="Notification Log",
			reference_name=log_name,
		)


def _sync_trial_inquiry_from_attendance(session_doc, row, previous_status, status, actor, comment):
	inquiry = _get_row_source(row, "Inquiry")
	if not inquiry or row.get("enrollment_type") != TRIAL_ENROLLMENT_TYPE:
		return
	if status not in PRESENT_STATUSES and status not in ABSENT_STATUSES:
		return

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		return

	target_status = "Completed" if status in PRESENT_STATUSES else "No-show"
	note = _build_inquiry_attendance_note(
		status=status,
		target_status=target_status,
		session_doc=session_doc,
		row=row,
		previous_status=previous_status,
		comment=comment,
	)

	if inquiry_doc.status in FINAL_INQUIRY_STATUSES or inquiry_doc.status == "Follow-up":
		_add_system_inquiry_note(inquiry_doc, note, row.name, actor)
		return

	if inquiry_doc.status != target_status:
		inquiry_doc.status = target_status
		inquiry_doc.save(ignore_permissions=True)

	_add_system_inquiry_note(inquiry_doc, note, row.name, actor)


def _get_row_source(row, doctype):
	if row.get("source_doctype") == doctype and row.get("source_document"):
		return row.get("source_document")
	if doctype == "Inquiry":
		return frappe.db.get_value("Inquiry", {"attendance_row_id": row.name}, "name")
	return None


def _add_system_inquiry_note(inquiry_doc, note, attendance_row_id, actor):
	note_doc = frappe.new_doc("Inquiry Note")
	note_doc.inquiry = inquiry_doc.name
	note_doc.student = inquiry_doc.student
	note_doc.note = note
	note_doc.author = actor or frappe.session.user
	note_doc.edited_at = now_datetime()
	_set_if_field(note_doc, "note_type", "System")
	_set_if_field(note_doc, "source_doctype", "Attendance Record")
	_set_if_field(note_doc, "source_document", attendance_row_id)
	note_doc.flags.ignore_permissions = True
	note_doc.insert()


def _get_attendance_context(session_doc, row):
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


def _build_inquiry_attendance_note(status, target_status, session_doc, row, previous_status=None, comment=None):
	parts = [
		_("Teacher attendance marked {0}.").format(status),
		_("Inquiry status synced to {0}.").format(target_status),
		_("Course Session: {0}.").format(session_doc.name),
	]
	if previous_status:
		parts.append(_("Previous attendance status: {0}.").format(previous_status))
	if comment:
		parts.append(_("Teacher comment: {0}").format(comment))
	return " ".join(parts)


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


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)
