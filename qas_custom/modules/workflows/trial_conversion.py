from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from qas_custom.modules.attendance.commands import create_full_term_attendance_entries
from qas_custom.modules.billing.commands import create_prorata_invoice, run_invoice_mutation_as_administrator
from qas_custom.modules.common import clear_frappe_messages
from qas_custom.modules.course_schedule.queries import (
	build_session_option,
	get_remaining_sessions,
	get_session_context,
)
from qas_custom.modules.enrollment.commands import (
	assert_no_active_full_term_enrollment,
	create_full_term_enrollment,
	link_invoice_to_enrollment,
)
from qas_custom.modules.inquiry.commands import get_inquiry_for_conversion, mark_converted
from qas_custom.modules.inquiry.notes import add_conversion_note, add_system_note


CONVERSION_INTERNAL_NOTE_MAX_LENGTH = 1000
LINKABLE_ENROLLMENT_STATUSES = {"Planned", "Active"}


def get_conversion_session_options(inquiry: str | None, start_date=None, course=None, campus=None, limit=80):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Only trial lesson inquiries can be converted."))
	if inquiry_doc.status not in {"Completed", "Follow-up"}:
		frappe.throw(_("Conversion sessions are only available after a trial lesson is completed."))

	session_date = getdate(start_date or nowdate())
	timeslot_filters = {"campus": campus or inquiry_doc.campus}
	if course:
		timeslot_filters["course"] = course
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters=timeslot_filters,
		fields=["name", "course", "class_language", "campus", "classroom", "teacher", "start_time", "end_time", "term"],
		order_by="course asc, start_time asc",
	)
	if not timeslots:
		return {"items": []}

	timeslot_map = {row.name: row for row in timeslots}
	sessions = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": ["in", list(timeslot_map.keys())],
			"session_date": session_date,
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, name asc",
		limit=limit,
	)
	items = []
	for session in sessions:
		timeslot = timeslot_map.get(session.weekly_timeslot)
		if not timeslot:
			continue
		items.append(build_session_option(session, timeslot))
	return {"items": items}


def convert_inquiry_to_full_term_core(
	inquiry: str | None,
	course_session: str | None,
	actor=None,
	internal_note=None,
):
	if not course_session:
		frappe.throw(_("Course session is required."))
	internal_note = normalize_conversion_internal_note(internal_note)

	inquiry_doc = get_inquiry_for_conversion(inquiry)
	context = get_session_context(course_session)
	session = context["session"]
	timeslot = context["timeslot"]
	course = timeslot.get("course")
	term = timeslot.get("term")
	if not course:
		frappe.throw(_("Selected session is missing a course."))
	if not term:
		frappe.throw(_("Selected session is missing a term."))

	assert_no_active_full_term_enrollment(inquiry_doc.student, term, course, timeslot.name)
	remaining_sessions = get_remaining_sessions(timeslot.name, session.session_date)
	if not remaining_sessions:
		frappe.throw(_("No remaining course sessions were found from the selected start session."))

	enrollment = create_full_term_enrollment(
		inquiry_doc=inquiry_doc,
		session=session,
		timeslot=timeslot,
		remaining_session_count=len(remaining_sessions),
		actor=actor,
	)
	clear_frappe_messages()
	invoice = create_prorata_invoice(
		inquiry_doc=inquiry_doc,
		enrollment=enrollment,
		course=course,
		term=term,
		start_session=session.name,
		remaining_session_count=len(remaining_sessions),
	)
	apply_conversion_invoice_note(invoice, internal_note)
	link_invoice_to_enrollment(enrollment, invoice)
	create_full_term_attendance_entries(remaining_sessions, inquiry_doc.student, enrollment.name)
	inquiry_doc = mark_converted(inquiry_doc, enrollment, invoice)
	add_conversion_note(
		inquiry_doc=inquiry_doc,
		enrollment=enrollment,
		invoice=invoice,
		session=session,
		timeslot=timeslot,
		remaining_session_count=len(remaining_sessions),
		actor=actor,
	)

	frappe.db.commit()

	from qas_custom.services.inquiry import build_inquiry_detail

	return {
		"inquiry": build_inquiry_detail(inquiry_doc.name),
		"conversion": {
			"enrollment": enrollment.name,
			"invoice": invoice.name,
			"remaining_sessions": len(remaining_sessions),
			"invoice_amount": flt(invoice.grand_total),
		},
	}


def link_existing_enrollment_core(inquiry: str | None, enrollment: str | None, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	if not enrollment:
		frappe.throw(_("Enrollment is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Only Trial Lesson inquiries can be converted."))

	converted_enrollment = str(inquiry_doc.get("converted_enrollment") or "").strip()
	if inquiry_doc.status == "Converted":
		if converted_enrollment == enrollment:
			from qas_custom.services.inquiry import build_inquiry_detail

			return build_inquiry_detail(inquiry_doc.name)
		frappe.throw(
			_("This Inquiry is already converted to Enrollment {0}.").format(
				converted_enrollment or _("Unknown")
			)
		)
	if converted_enrollment:
		frappe.throw(_("This Inquiry already references converted Enrollment {0}.").format(converted_enrollment))
	if inquiry_doc.status not in {"Completed", "Follow-up"}:
		frappe.throw(_("Only Completed or Follow-up inquiries can be linked to an existing Enrollment."))
	if not inquiry_doc.get("student"):
		frappe.throw(_("Student is required before converting a Trial Lesson Inquiry."))
	if not inquiry_doc.get("parent"):
		frappe.throw(_("Parent is required before converting a Trial Lesson Inquiry."))

	enrollment_doc = frappe.get_doc("Enrollment", enrollment)
	if enrollment_doc.get("status") not in LINKABLE_ENROLLMENT_STATUSES:
		frappe.throw(_("The existing Enrollment must be Planned or Active."))
	if enrollment_doc.get("student") != inquiry_doc.student:
		frappe.throw(_("The existing Enrollment must belong to the same Student as the Inquiry."))
	if enrollment_doc.get("parent") and inquiry_doc.get("parent") and enrollment_doc.parent != inquiry_doc.parent:
		frappe.throw(_("The existing Enrollment must belong to the same Parent as the Inquiry."))

	source_inquiry = str(enrollment_doc.get("source_inquiry") or "").strip()
	if source_inquiry and source_inquiry != inquiry_doc.name:
		frappe.throw(
			_("Enrollment {0} is already linked to Inquiry {1}.").format(enrollment_doc.name, source_inquiry)
		)
	conflicting_inquiry = frappe.db.get_value(
		"Inquiry",
		{
			"name": ["!=", inquiry_doc.name],
			"status": "Converted",
			"converted_enrollment": enrollment_doc.name,
		},
		"name",
	)
	if conflicting_inquiry:
		frappe.throw(
			_("Enrollment {0} is already used by converted Inquiry {1}.").format(
				enrollment_doc.name,
				conflicting_inquiry,
			)
		)

	if enrollment_doc.meta.has_field("source_inquiry") and not source_inquiry:
		enrollment_doc.source_inquiry = inquiry_doc.name
		enrollment_doc.save(ignore_permissions=True)

	inquiry_doc.status = "Converted"
	inquiry_doc.converted_enrollment = enrollment_doc.name
	if inquiry_doc.meta.has_field("converted_invoice"):
		inquiry_doc.converted_invoice = enrollment_doc.get("invoice") or ""
	inquiry_doc.save(ignore_permissions=True)
	add_system_note(
		inquiry_doc=inquiry_doc,
		note=_(
			"School Admin linked existing Enrollment {0}; no new Enrollment, invoice, or attendance was created."
		).format(enrollment_doc.name),
		source_doctype="Enrollment",
		source_document=enrollment_doc.name,
		actor=actor,
	)
	frappe.db.commit()

	from qas_custom.services.inquiry import build_inquiry_detail

	return build_inquiry_detail(inquiry_doc.name)


def normalize_conversion_internal_note(internal_note):
	note = str(internal_note or "").strip()
	if len(note) > CONVERSION_INTERNAL_NOTE_MAX_LENGTH:
		frappe.throw(
			_("Internal note cannot exceed {0} characters.").format(CONVERSION_INTERNAL_NOTE_MAX_LENGTH)
		)
	return note


def append_conversion_invoice_note(existing_remarks, internal_note):
	note_line = _("Campus Admin conversion note: {0}").format(internal_note)
	existing = str(existing_remarks or "").rstrip()
	return "{0}\n{1}".format(existing, note_line) if existing else note_line


def apply_conversion_invoice_note(invoice, internal_note):
	if not internal_note:
		return invoice
	invoice.set("remarks", append_conversion_invoice_note(invoice.get("remarks"), internal_note))
	run_invoice_mutation_as_administrator(lambda: invoice.save(ignore_permissions=True))
	return invoice
