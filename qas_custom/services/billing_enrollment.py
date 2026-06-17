from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate


FULL_TERM = "Full-Term"
ACTIVE = "Active"
DEFAULT_ATTENDANCE_STATUS = "To be started"


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
		fields=["name", "course", "campus", "classroom", "teacher", "start_time", "end_time", "term"],
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
		items.append(_build_session_option(session, timeslot))
	return {"items": items}


def convert_inquiry_to_full_term_core(inquiry: str | None, course_session: str | None, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	if not course_session:
		frappe.throw(_("Course session is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Only trial lesson inquiries can be converted."))
	if inquiry_doc.status not in {"Completed", "Follow-up"}:
		frappe.throw(_("Only completed trial lessons can be converted."))
	if inquiry_doc.converted_enrollment:
		frappe.throw(_("This inquiry has already been converted."))
	if not inquiry_doc.student:
		frappe.throw(_("Student is required before converting a trial lesson."))
	if not inquiry_doc.parent:
		frappe.throw(_("Parent is required before converting a trial lesson."))

	context = _get_session_context(course_session)
	session = context["session"]
	timeslot = context["timeslot"]
	course = timeslot.get("course")
	term = timeslot.get("term")
	if not course:
		frappe.throw(_("Selected session is missing a course."))
	if not term:
		frappe.throw(_("Selected session is missing a term."))

	_existing_enrollment_guard(inquiry_doc.student, term, course, timeslot.name)
	remaining_sessions = _get_remaining_sessions(timeslot.name, session.session_date)
	if not remaining_sessions:
		frappe.throw(_("No remaining course sessions were found from the selected start session."))

	enrollment = _create_full_term_enrollment(
		inquiry_doc=inquiry_doc,
		session=session,
		timeslot=timeslot,
		remaining_session_count=len(remaining_sessions),
		actor=actor,
	)
	invoice = _create_prorata_invoice(
		inquiry_doc=inquiry_doc,
		enrollment=enrollment,
		course=course,
		term=term,
		start_session=session.name,
		remaining_session_count=len(remaining_sessions),
	)
	_link_invoice_to_enrollment(enrollment, invoice)
	_add_full_term_attendance_rows(remaining_sessions, inquiry_doc.student, enrollment.name)

	inquiry_doc.status = "Converted"
	inquiry_doc.converted_enrollment = enrollment.name
	if inquiry_doc.meta.has_field("converted_invoice"):
		inquiry_doc.converted_invoice = invoice.name
	inquiry_doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {
		"inquiry": _build_inquiry_detail(inquiry_doc.name),
		"conversion": {
			"enrollment": enrollment.name,
			"invoice": invoice.name,
			"remaining_sessions": len(remaining_sessions),
			"invoice_amount": flt(invoice.grand_total),
		},
	}


def mark_inquiry_inactive_core(inquiry: str | None, inactive_reason: str | None, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	reason = (inactive_reason or "").strip()
	if not reason:
		frappe.throw(_("Inactive reason is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status == "Converted":
		frappe.throw(_("A converted inquiry cannot be marked inactive."))
	if inquiry_doc.status not in {"Completed", "Follow-up", "No-show"}:
		frappe.throw(_("Only post-trial inquiries can be marked inactive."))

	inquiry_doc.status = "Inactive"
	inquiry_doc.inactive_reason = reason
	inquiry_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _build_inquiry_detail(inquiry_doc.name)


def _get_session_context(course_session: str):
	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "status"],
		as_dict=True,
	)
	if not session:
		frappe.throw(_("Course session was not found."))
	if not session.get("weekly_timeslot"):
		frappe.throw(_("Course session is missing a weekly timeslot."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.weekly_timeslot,
		["name", "term", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))
	return {"session": session, "timeslot": timeslot}


def _existing_enrollment_guard(student: str, term: str, course: str, weekly_timeslot: str):
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


def _get_remaining_sessions(weekly_timeslot: str, start_date):
	return frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": weekly_timeslot,
			"session_date": [">=", getdate(start_date)],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, name asc",
	)


def _create_full_term_enrollment(inquiry_doc, session, timeslot, remaining_session_count: int, actor=None):
	enrollment = frappe.new_doc("Enrollment")
	_set_if_field(enrollment, "student", inquiry_doc.student)
	_set_if_field(enrollment, "parent", inquiry_doc.parent)
	_set_if_field(enrollment, "term", timeslot.term)
	_set_if_field(enrollment, "course", timeslot.course)
	_set_if_field(enrollment, "weekly_timeslot", timeslot.name)
	_set_if_field(enrollment, "course_session", session.name)
	_set_if_field(enrollment, "start_course_session", session.name)
	_set_if_field(enrollment, "enrollment_type", FULL_TERM)
	_set_if_field(enrollment, "status", ACTIVE)
	_set_if_field(enrollment, "enrollment_date", getdate(session.session_date))
	_set_if_field(enrollment, "source_inquiry", inquiry_doc.name)
	_set_if_field(enrollment, "remaining_sessions", remaining_session_count)
	_set_if_field(enrollment, "created_by_conversion_user", actor or frappe.session.user)
	enrollment.insert(ignore_permissions=True)
	return enrollment


def _create_prorata_invoice(inquiry_doc, enrollment, course: str, term: str, start_session: str, remaining_session_count: int):
	full_term_fee = _get_course_money(course, ("full_term_fee", "full_term_price", "term_fee"))
	total_sessions = _get_course_number(course, ("total_session_per_term", "total_sessions_per_term", "sessions_per_term"))
	if full_term_fee <= 0:
		frappe.throw(_("Course full term fee is required before generating a pro rata invoice."))
	if total_sessions <= 0:
		frappe.throw(_("Course total sessions per term is required before generating a pro rata invoice."))

	unit_rate = flt(full_term_fee) / flt(total_sessions)
	customer = _get_invoice_customer(inquiry_doc.parent)
	item_code = _get_invoice_item(course)

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.due_date = nowdate()
	_set_if_field(invoice, "student", inquiry_doc.student)
	_set_if_field(invoice, "parent", inquiry_doc.parent)
	_set_if_field(invoice, "enrollment", enrollment.name)
	_set_if_field(invoice, "course", course)
	_set_if_field(invoice, "term", term)
	_set_if_field(invoice, "source_inquiry", inquiry_doc.name)
	invoice.append(
		"items",
		{
			"item_code": item_code,
			"item_name": course,
			"description": _("Pro rata enrollment for {0} from session {1}").format(course, start_session),
			"qty": remaining_session_count,
			"rate": unit_rate,
		},
	)
	invoice.insert(ignore_permissions=True)
	return invoice


def _link_invoice_to_enrollment(enrollment, invoice):
	updates = {}
	if enrollment.meta.has_field("invoice"):
		updates["invoice"] = invoice.name
	if enrollment.meta.has_field("invoice_status"):
		updates["invoice_status"] = "Draft"
	if enrollment.meta.has_field("invoice_amount"):
		updates["invoice_amount"] = flt(invoice.grand_total)
	if updates:
		frappe.db.set_value("Enrollment", enrollment.name, updates, update_modified=False)


def _add_full_term_attendance_rows(sessions, student: str, enrollment: str):
	for session in sessions:
		session_doc = frappe.get_doc("Course Sessions", session.name)
		exists = False
		for row in session_doc.get("attendance_list", []):
			if row.student == student and row.enrollment_type == FULL_TERM:
				exists = True
				break
		if exists:
			continue
		session_doc.append(
			"attendance_list",
			{
				"student": student,
				"enrollment_type": FULL_TERM,
				"status": DEFAULT_ATTENDANCE_STATUS,
				"comments": f"Added from Enrollment {enrollment}",
			},
		)
		session_doc.save(ignore_permissions=True)


def _get_course_money(course: str, fieldnames: tuple[str, ...]):
	for fieldname in fieldnames:
		if frappe.db.has_column("Course", fieldname):
			return flt(frappe.db.get_value("Course", course, fieldname) or 0)
	return 0


def _get_course_number(course: str, fieldnames: tuple[str, ...]):
	for fieldname in fieldnames:
		if frappe.db.has_column("Course", fieldname):
			return flt(frappe.db.get_value("Course", course, fieldname) or 0)
	return 0


def _get_invoice_customer(parent: str):
	if not frappe.db.has_column("Parent", "customer"):
		frappe.throw(_("Parent is missing a Customer field for invoicing."))
	customer = frappe.db.get_value("Parent", parent, "customer")
	if not customer:
		frappe.throw(_("Parent is missing a linked Customer for invoicing."))
	return customer


def _get_invoice_item(course: str):
	course_item = _get_course_invoice_item(course)
	if course_item:
		return course_item

	configured = (
		frappe.conf.get("qas_full_term_invoice_item")
		or frappe.conf.get("qas_enrollment_invoice_item")
		or frappe.conf.get("qas_default_invoice_item")
	)
	if configured and frappe.db.exists("Item", configured):
		return configured
	if frappe.db.exists("Item", course):
		return course
	frappe.throw(
		_(
			"Course invoice item is not configured. Set Invoice Item on the Course, set qas_full_term_invoice_item, or create an Item matching the Course name."
		)
	)


def _get_course_invoice_item(course: str):
	if not frappe.db.has_column("Course", "invoice_item"):
		return None
	item = frappe.db.get_value("Course", course, "invoice_item")
	if not item:
		return None
	if not frappe.db.exists("Item", item):
		frappe.throw(_("Course Invoice Item does not exist: {0}").format(item))
	return item


def _set_if_field(doc, fieldname: str, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _build_session_option(session, timeslot):
	return {
		"id": session.name,
		"name": session.name,
		"course_session": session.name,
		"session_date": str(session.session_date) if session.session_date else None,
		"course": timeslot.course,
		"campus": timeslot.campus,
		"classroom": timeslot.classroom,
		"teacher": timeslot.teacher,
		"term": timeslot.term,
		"start_time": str(timeslot.start_time) if timeslot.start_time else None,
		"end_time": str(timeslot.end_time) if timeslot.end_time else None,
		"label": " · ".join(
			str(part)
			for part in [
				session.session_date,
				timeslot.start_time,
				timeslot.course,
				timeslot.campus,
				timeslot.teacher,
			]
			if part
		),
	}


def _build_inquiry_detail(inquiry: str):
	from qas_custom.services.inquiry import build_inquiry_detail

	return build_inquiry_detail(inquiry)
