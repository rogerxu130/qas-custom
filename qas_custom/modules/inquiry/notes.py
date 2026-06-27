from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from qas_custom.modules.common import set_if_field


def add_conversion_note(inquiry_doc, enrollment, invoice, session, timeslot, remaining_session_count: int, actor=None):
	note_doc = frappe.new_doc("Inquiry Note")
	note_doc.inquiry = inquiry_doc.name
	note_doc.student = inquiry_doc.student
	note_doc.note = build_conversion_note(
		enrollment=enrollment,
		invoice=invoice,
		session=session,
		timeslot=timeslot,
		remaining_session_count=remaining_session_count,
	)
	note_doc.author = actor or frappe.session.user
	note_doc.edited_at = now_datetime()
	set_if_field(note_doc, "note_type", "System")
	set_if_field(note_doc, "source_doctype", "Enrollment")
	set_if_field(note_doc, "source_document", enrollment.name)
	note_doc.flags.ignore_permissions = True
	note_doc.insert()


def add_attendance_sync_note(inquiry_doc, attendance_entry, status, target_status, previous_status=None, comment=None, actor=None):
	add_system_note(
		inquiry_doc=inquiry_doc,
		note=build_attendance_sync_note(
			status=status,
			target_status=target_status,
			course_session=attendance_entry.course_session,
			previous_status=previous_status,
			comment=comment,
		),
		source_doctype="Class Attendance Entry",
		source_document=attendance_entry.name,
		actor=actor,
	)


def add_system_note(inquiry_doc, note, source_doctype=None, source_document=None, actor=None):
	note_doc = frappe.new_doc("Inquiry Note")
	note_doc.inquiry = inquiry_doc.name
	note_doc.student = inquiry_doc.student
	note_doc.note = note
	note_doc.author = actor or frappe.session.user
	note_doc.edited_at = now_datetime()
	set_if_field(note_doc, "note_type", "System")
	set_if_field(note_doc, "source_doctype", source_doctype)
	set_if_field(note_doc, "source_document", source_document)
	note_doc.flags.ignore_permissions = True
	note_doc.insert()


def build_conversion_note(enrollment, invoice, session, timeslot, remaining_session_count: int):
	start_date = session.session_date if session and session.get("session_date") else None
	start_time = timeslot.start_time if timeslot and timeslot.get("start_time") else None
	parts = [
		_("Trial converted to full-term enrollment."),
		_("Course: {0}").format(timeslot.course),
		_("Term: {0}").format(timeslot.term),
		_("Start session: {0}").format(session.name),
	]
	if start_date:
		parts.append(_("Start date: {0}").format(start_date))
	if start_time:
		parts.append(_("Start time: {0}").format(start_time))
	parts.extend(
		[
			_("Remaining sessions: {0}").format(remaining_session_count),
			_("Enrollment: {0}").format(enrollment.name),
			_("Draft invoice: {0}").format(invoice.name),
			_("Draft invoice amount: {0}").format(flt(invoice.grand_total)),
		]
	)
	return " ".join(str(part) for part in parts if part)


def build_attendance_sync_note(status, target_status, course_session, previous_status=None, comment=None):
	parts = [
		_("Teacher attendance marked {0}.").format(status),
		_("Inquiry status synced to {0}.").format(target_status),
		_("Course Session: {0}.").format(course_session),
	]
	if previous_status:
		parts.append(_("Previous attendance status: {0}.").format(previous_status))
	if comment:
		parts.append(_("Teacher comment: {0}").format(comment))
	return " ".join(parts)
