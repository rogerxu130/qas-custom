from __future__ import annotations

import frappe
from frappe.utils import flt, formatdate

from qas_custom.modules.billing.store_credit import (
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
)
from qas_custom.modules.billing.invoice_settings import get_invoice_payment_context
from qas_custom.services.display_labels import get_student_parent_name

DEFAULT_PARENT_PORTAL_URL = "https://portal.queenslandartschool.com"


def build_course_invoice_description(student_name: str, course: str, term: str, session_count: int) -> str:
	parts = [course]
	if term:
		parts.append(f"({term})")
	count = int(session_count or 0)
	session_label = "1 session" if count == 1 else f"{count} sessions"
	return f"{student_name} - {' '.join(parts)}, {session_label}"


def get_invoice_print_context(invoice_doc):
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	store_credit_applied = get_invoice_store_credit_applied(doc.name)
	return build_parent_invoice_context(
		doc,
		store_credit_applied=store_credit_applied,
		payable_amount=get_invoice_payable_amount(doc),
		invoice_link=parent_portal_invoice_link(doc.name),
	)


def build_parent_invoice_context(invoice_doc, *, store_credit_applied=None, payable_amount=None, payment_link=None, invoice_link=None):
	store_credit = flt(store_credit_applied if store_credit_applied is not None else get_invoice_store_credit_applied(invoice_doc.name))
	payable = flt(payable_amount if payable_amount is not None else get_invoice_payable_amount(invoice_doc))
	portal_link = invoice_link or payment_link or parent_portal_invoice_link(invoice_doc.name)
	payment_context = get_invoice_payment_context(invoice_doc)
	return {
		"invoice": invoice_doc.name,
		"customer": invoice_doc.get("customer_name") or invoice_doc.get("customer") or "",
		"parent": invoice_doc.get("parent") or "",
		"posting_date": formatdate(invoice_doc.get("posting_date")) if invoice_doc.get("posting_date") else "",
		"due_date": formatdate(invoice_doc.get("due_date")) if invoice_doc.get("due_date") else "",
		"total": flt(invoice_doc.get("grand_total") or invoice_doc.get("rounded_total") or 0),
		"store_credit_applied": store_credit,
		"payable_amount": payable,
		"invoice_link": portal_link,
		"payment_link": portal_link,
		**payment_context,
		"items": [_build_parent_invoice_item(row) for row in invoice_doc.get("items", [])],
	}


def parent_portal_invoice_link(invoice: str):
	from urllib.parse import urlencode

	base_url = (
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or DEFAULT_PARENT_PORTAL_URL
	)
	return f"{str(base_url).rstrip('/')}/invoices?{urlencode({'invoice': invoice})}"


def _build_parent_invoice_item(row):
	student = row.get("student") if hasattr(row, "get") else None
	student_display_name = row.get("student_display_name") if hasattr(row, "get") else None
	student_label = student_display_name or get_student_parent_name(student) or student or "Student"
	course = row.get("course") or row.get("item_name") or row.get("item_code") or "Course"
	term = row.get("term") or ""
	session_count = int(flt(row.get("session_count") or row.get("qty") or 0))
	quantity = flt(row.get("qty") or 0)
	rate = flt(row.get("rate") or 0)
	amount = flt(row.get("amount") or quantity * rate)

	detail_parts = [course]
	if term:
		detail_parts.append(term)
	if session_count:
		detail_parts.append("1 session" if session_count == 1 else f"{session_count} sessions")

	return {
		"student": student_label,
		"course": course,
		"term": term,
		"sessions": session_count or quantity,
		"rate": rate,
		"amount": amount,
		"description": " - ".join(str(part) for part in detail_parts if part),
	}
