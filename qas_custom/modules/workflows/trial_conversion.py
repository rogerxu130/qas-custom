from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from qas_custom.modules.attendance.commands import create_full_term_attendance_entries
from qas_custom.modules.billing.commands import create_prorata_invoice
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
from qas_custom.modules.inquiry.notes import add_conversion_note


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


def convert_inquiry_to_full_term_core(inquiry: str | None, course_session: str | None, actor=None):
	if not course_session:
		frappe.throw(_("Course session is required."))

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
