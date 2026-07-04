from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import flt, formatdate, get_time, getdate

from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts
from qas_custom.modules.billing.invoice_settings import get_invoice_payment_context, get_invoice_settings
from qas_custom.services.display_labels import get_student_parent_name

DEFAULT_PARENT_PORTAL_URL = "https://portal.queenslandartschool.com"


def build_course_invoice_description(student_name: str, course: str, term: str, session_count: int, schedule: str | None = None) -> str:
	course_label = _course_term_label(course, term)
	count = int(session_count or 0)
	session_label = "1 session" if count == 1 else f"{count} sessions"
	parts = [course_label, schedule, session_label]
	return f"{student_name} - {' - '.join(str(part) for part in parts if part)}"


def get_invoice_print_context(invoice_doc):
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	amounts = resolve_invoice_print_amounts(doc)
	return build_parent_invoice_context(
		doc,
		store_credit_applied=amounts["store_credit_applied"],
		payable_amount=amounts["payable_amount"],
		invoice_link=parent_portal_invoice_link(doc.name),
	)


def build_parent_invoice_context(
	invoice_doc,
	*,
	store_credit_applied=None,
	payable_amount=None,
	payment_link=None,
	invoice_link=None,
	include_portal_link=True,
):
	if store_credit_applied is None or payable_amount is None:
		amounts = resolve_invoice_print_amounts(
			invoice_doc,
			store_credit_applied=store_credit_applied,
			payable_amount=payable_amount,
		)
		store_credit_applied = amounts["store_credit_applied"]
		payable_amount = amounts["payable_amount"]
	store_credit = flt(store_credit_applied)
	payable = flt(payable_amount)
	portal_link = (invoice_link or payment_link or parent_portal_invoice_link(invoice_doc.name)) if include_portal_link else ""
	payment_context = get_invoice_payment_context(invoice_doc)
	settings = get_invoice_settings()
	return {
		"invoice": invoice_doc.name,
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_logo": settings.get("school_logo") or "",
		"school_logo_url": settings.get("school_logo_url") or "",
		"legal_name": settings.get("legal_name") or "",
		"abn": settings.get("abn") or "",
		"customer": invoice_doc.get("customer_name") or invoice_doc.get("customer") or "",
		"parent": invoice_doc.get("parent") or "",
		"recipient_name": _invoice_recipient_name(invoice_doc),
		"posting_date": formatdate(invoice_doc.get("posting_date")) if invoice_doc.get("posting_date") else "",
		"due_date": formatdate(invoice_doc.get("due_date")) if invoice_doc.get("due_date") else "",
		"total": flt(invoice_doc.get("grand_total") or invoice_doc.get("rounded_total") or 0),
		"store_credit_applied": store_credit,
		"payable_amount": payable,
		"invoice_link": portal_link,
		"payment_link": portal_link,
		**payment_context,
		"items": [build_parent_invoice_item(row) for row in invoice_doc.get("items", [])],
	}


def _invoice_recipient_name(invoice_doc):
	parent = invoice_doc.get("parent") or _parent_for_invoice_customer(invoice_doc.get("customer"))
	if parent and frappe.db.has_column("Parent", "parent_name"):
		parent_name = frappe.db.get_value("Parent", parent, "parent_name")
		if parent_name:
			return parent_name

	customer_name = invoice_doc.get("customer_name")
	if customer_name:
		return customer_name

	customer = invoice_doc.get("customer")
	if customer and frappe.db.has_column("Customer", "customer_name"):
		return frappe.db.get_value("Customer", customer, "customer_name") or customer

	return ""


def _parent_for_invoice_customer(customer):
	if not customer or not frappe.db.exists("DocType", "Parent") or not frappe.db.has_column("Parent", "customer"):
		return None
	return frappe.db.get_value("Parent", {"customer": customer}, "name")


def parent_portal_invoice_link(invoice: str):
	from urllib.parse import urlencode

	base_url = (
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or DEFAULT_PARENT_PORTAL_URL
	)
	return f"{str(base_url).rstrip('/')}/invoices?{urlencode({'invoice': invoice})}"


def build_parent_invoice_item(row):
	student = row.get("student") if hasattr(row, "get") else None
	student_display_name = row.get("student_display_name") if hasattr(row, "get") else None
	student_label = student_display_name or get_student_parent_name(student) or student or "Student"
	course = row.get("course") or row.get("item_name") or row.get("item_code") or "Course"
	term = row.get("term") or ""
	session_count = int(flt(row.get("session_count") or row.get("qty") or 0))
	quantity = flt(row.get("qty") or 0)
	rate = flt(row.get("rate") or 0)
	amount = flt(row.get("amount") or quantity * rate)
	schedule = invoice_item_schedule(row)

	detail_parts = [_course_term_label(course, term)]
	if schedule:
		detail_parts.append(schedule)
	if session_count:
		detail_parts.append("1 session" if session_count == 1 else f"{session_count} sessions")

	return {
		"item_code": row.get("item_code") if hasattr(row, "get") else None,
		"student": student_label,
		"student_display_name": student_display_name,
		"enrollment": row.get("enrollment") if hasattr(row, "get") else None,
		"course": course,
		"term": term,
		"course_session": row.get("course_session") if hasattr(row, "get") else None,
		"session_count": row.get("session_count") if hasattr(row, "get") else None,
		"qas_line_type": row.get("qas_line_type") if hasattr(row, "get") else None,
		"schedule": schedule,
		"sessions": session_count or quantity,
		"rate": rate,
		"amount": amount,
		"description": " - ".join(str(part) for part in detail_parts if part),
	}


def invoice_item_schedule(row) -> str:
	if not hasattr(row, "get"):
		return ""

	weekly_timeslot = row.get("weekly_timeslot")
	if weekly_timeslot:
		schedule = _schedule_for_weekly_timeslot(weekly_timeslot)
		if schedule:
			return schedule

	enrollment = row.get("enrollment")
	if enrollment:
		schedule = _schedule_for_enrollment(enrollment)
		if schedule:
			return schedule

	course_session = _course_session_name(row.get("course_session"))
	if course_session:
		schedule = _schedule_for_course_session(course_session)
		if schedule:
			return schedule

	return ""


def _course_term_label(course: str, term: str | None) -> str:
	if term:
		return f"{course} ({term})"
	return course or "Course"


def _schedule_for_enrollment(enrollment: str) -> str:
	weekly_timeslot = frappe.db.get_value("Enrollment", enrollment, "weekly_timeslot")
	return _schedule_for_weekly_timeslot(weekly_timeslot)


def _schedule_for_course_session(course_session: str) -> str:
	if not frappe.db.exists("Course Sessions", course_session):
		return ""
	weekly_timeslot = frappe.db.get_value("Course Sessions", course_session, "weekly_timeslot")
	return _schedule_for_weekly_timeslot(weekly_timeslot)


def _schedule_for_weekly_timeslot(weekly_timeslot: str | None) -> str:
	if not weekly_timeslot:
		return ""
	row = frappe.db.get_value(
		"Weekly Timeslot",
		weekly_timeslot,
		["day_of_week", "start_time", "end_time"],
		as_dict=True,
	)
	if not row:
		return ""

	start_time = _format_schedule_time(row.get("start_time"))
	end_time = _format_schedule_time(row.get("end_time"))
	if start_time and end_time:
		time_label = f"{start_time}-{end_time}"
	else:
		time_label = start_time or end_time
	return " ".join(str(part) for part in (row.get("day_of_week"), time_label) if part)


def _course_session_name(value) -> str:
	text = str(value or "").strip()
	if " · " in text:
		text = text.split(" · ", 1)[0].strip()
	return text


def _format_schedule_time(value) -> str:
	if not value:
		return ""
	try:
		time_value = get_time(value)
	except Exception:
		return str(value)
	return datetime.combine(getdate(), time_value).strftime("%I:%M %p").lstrip("0")
