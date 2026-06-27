from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from qas_custom.modules.common import set_if_field


ACTIVE = "Active"
FULL_TERM = "Full-Term"


def assert_no_active_full_term_enrollment(student: str, term: str, course: str, weekly_timeslot: str):
	existing = frappe.db.exists(
		"Enrollment",
		{
			"student": student,
			"term": term,
			"course": course,
			"weekly_timeslot": weekly_timeslot,
			"status": ACTIVE,
		},
	)
	if existing:
		frappe.throw(_("This student already has an active enrollment for the selected class: {0}").format(existing))


def create_full_term_enrollment(inquiry_doc, session, timeslot, remaining_session_count: int, actor=None):
	enrollment = frappe.new_doc("Enrollment")
	set_if_field(enrollment, "student", inquiry_doc.student)
	set_if_field(enrollment, "parent", inquiry_doc.parent)
	set_if_field(enrollment, "term", timeslot.term)
	set_if_field(enrollment, "course", timeslot.course)
	set_if_field(enrollment, "weekly_timeslot", timeslot.name)
	set_if_field(enrollment, "start_course_session", session.name)
	set_if_field(enrollment, "enrollment_type", FULL_TERM)
	set_if_field(enrollment, "status", ACTIVE)
	set_if_field(enrollment, "enrollment_date", getdate(session.session_date))
	set_if_field(enrollment, "source_inquiry", inquiry_doc.name)
	set_if_field(enrollment, "remaining_sessions", remaining_session_count)
	set_if_field(enrollment, "created_by_conversion_user", actor or frappe.session.user)
	enrollment.insert(ignore_permissions=True)
	return enrollment


def link_invoice_to_enrollment(enrollment, invoice):
	updates = {}
	if enrollment.meta.has_field("invoice"):
		updates["invoice"] = invoice.name
	if enrollment.meta.has_field("invoice_status"):
		updates["invoice_status"] = "Draft"
	if enrollment.meta.has_field("invoice_amount"):
		updates["invoice_amount"] = flt(invoice.grand_total)
	if updates:
		frappe.db.set_value("Enrollment", enrollment.name, updates, update_modified=False)
