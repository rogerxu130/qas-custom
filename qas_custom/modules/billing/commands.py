from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from qas_custom.modules.common import has_field, is_new_doc, set_if_field
from qas_custom.modules.billing.presentation import build_course_invoice_description
from qas_custom.modules.billing.invoice_settings import apply_invoice_payment_snapshot
from qas_custom.services.display_labels import get_student_display_code, get_student_parent_name


def create_prorata_invoice(inquiry_doc, enrollment, course: str, term: str, start_session: str, remaining_session_count: int):
	context = get_prorata_invoice_context(
		inquiry_doc=inquiry_doc,
		course=course,
		remaining_session_count=remaining_session_count,
	)
	customer = context["customer"]
	item_code = context["item_code"]
	unit_rate = context["unit_rate"]

	invoice = get_or_create_course_invoice(customer, inquiry_doc.parent)
	set_if_field(invoice, "parent", inquiry_doc.parent)
	set_if_field(invoice, "qas_invoice_type", "Course")
	set_course_invoice_source(invoice, inquiry_doc.name)
	set_if_field(invoice, "source_inquiry", inquiry_doc.name)

	student_name = get_student_parent_name(inquiry_doc.student) or inquiry_doc.student
	student_code = get_student_display_code(inquiry_doc.student) or inquiry_doc.student
	description = build_course_invoice_description(student_name, course, term, remaining_session_count)
	item = invoice.append(
		"items",
		{
			"item_code": item_code,
			"item_name": course,
			"description": description,
			"qty": remaining_session_count,
			"rate": unit_rate,
		},
	)
	set_if_field(item, "qas_line_type", "Course Fee")
	set_if_field(item, "student", inquiry_doc.student)
	set_if_field(item, "student_display_name", student_name)
	set_if_field(item, "student_code", student_code)
	set_if_field(item, "enrollment", enrollment.name)
	set_if_field(item, "course", course)
	set_if_field(item, "term", term)
	set_if_field(item, "course_session", start_session)
	set_if_field(item, "session_count", remaining_session_count)
	sync_invoice_student_summary(invoice)
	normalize_course_invoice_dates(invoice)
	apply_invoice_payment_snapshot(invoice)

	if is_new_doc(invoice):
		invoice.insert(ignore_permissions=True)
	else:
		invoice.save(ignore_permissions=True)
	return invoice


def get_prorata_invoice_context(inquiry_doc, course: str, remaining_session_count: int):
	full_term_fee = get_course_money(course, ("full_term_fee", "full_term_price", "term_fee"))
	total_sessions = get_course_number(course, ("total_session_per_term", "total_sessions_per_term", "sessions_per_term"))
	if full_term_fee <= 0:
		frappe.throw(_("Course full term fee is required before generating a pro rata invoice."))
	if total_sessions <= 0:
		frappe.throw(_("Course total sessions per term is required before generating a pro rata invoice."))
	return {
		"customer": get_invoice_customer(inquiry_doc.parent),
		"item_code": get_invoice_item(course),
		"unit_rate": flt(full_term_fee) / flt(total_sessions),
		"remaining_session_count": remaining_session_count,
	}


def set_course_invoice_source(invoice, inquiry: str):
	current_source = invoice.get("source_document") if hasattr(invoice, "get") else None
	if not current_source:
		set_if_field(invoice, "source_doctype", "Inquiry")
		set_if_field(invoice, "source_document", inquiry)
		set_if_field(invoice, "billing_note", _("Draft course invoice generated from trial conversion."))
	elif current_source != inquiry:
		set_if_field(
			invoice,
			"billing_note",
			_("Draft course invoice contains multiple course billing items. See item rows for student and enrollment details."),
		)


def get_or_create_course_invoice(customer: str, parent: str | None = None):
	filters = {"customer": customer, "docstatus": 0}
	if parent and has_field("Sales Invoice", "parent"):
		filters["parent"] = parent
	if has_field("Sales Invoice", "qas_invoice_type"):
		filters["qas_invoice_type"] = "Course"
	if has_field("Sales Invoice", "status"):
		filters["status"] = ["!=", "Cancelled"]

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name"],
		order_by="modified desc",
		limit=1,
	)
	if rows:
		return frappe.get_doc("Sales Invoice", rows[0].name)

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.due_date = nowdate()
	set_if_field(invoice, "parent", parent)
	set_if_field(invoice, "qas_invoice_type", "Course")
	apply_invoice_payment_snapshot(invoice)
	return invoice


def normalize_course_invoice_dates(invoice):
	today = nowdate()
	invoice.posting_date = today
	invoice.due_date = today
	for row in invoice.get("payment_schedule", []):
		row.due_date = today


def sync_invoice_student_summary(invoice):
	students = []
	seen = set()
	for item in invoice.get("items", []):
		student = item.get("student") if hasattr(item, "get") else None
		if student and student not in seen:
			seen.add(student)
			students.append(student)
	if not students:
		return

	set_if_field(invoice, "primary_student", students[0])
	labels = [get_student_parent_name(student) or student for student in students]
	summary = labels[0] if len(labels) == 1 else _("Multiple students: {0}").format(", ".join(labels))
	set_if_field(invoice, "student_summary", summary)


def get_course_money(course: str, fieldnames: tuple[str, ...]):
	for fieldname in fieldnames:
		if frappe.db.has_column("Course", fieldname):
			return flt(frappe.db.get_value("Course", course, fieldname) or 0)
	return 0


def get_course_number(course: str, fieldnames: tuple[str, ...]):
	for fieldname in fieldnames:
		if frappe.db.has_column("Course", fieldname):
			return flt(frappe.db.get_value("Course", course, fieldname) or 0)
	return 0


def get_invoice_customer(parent: str):
	if not frappe.db.has_column("Parent", "customer"):
		frappe.throw(_("Parent is missing a Customer field for invoicing."))
	customer = frappe.db.get_value("Parent", parent, "customer")
	if not customer:
		frappe.throw(_("Parent is missing a linked Customer for invoicing."))
	return customer


def get_invoice_item(course: str):
	course_item = get_course_invoice_item(course)
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


def get_course_invoice_item(course: str):
	if not frappe.db.has_column("Course", "invoice_item"):
		return None
	item = frappe.db.get_value("Course", course, "invoice_item")
	if not item:
		return None
	if not frappe.db.exists("Item", item):
		frappe.throw(_("Course Invoice Item does not exist: {0}").format(item))
	return item
