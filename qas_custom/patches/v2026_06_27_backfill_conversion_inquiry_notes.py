from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


def execute():
	if not frappe.db.exists("DocType", "Inquiry Note"):
		return
	if not frappe.db.exists("DocType", "Enrollment"):
		return

	fields = ["name", "student", "converted_enrollment"]
	if frappe.db.has_column("Inquiry", "converted_invoice"):
		fields.append("converted_invoice")
	rows = frappe.get_all(
		"Inquiry",
		filters={"status": "Converted", "converted_enrollment": ["is", "set"]},
		fields=fields,
		limit_page_length=0,
	)
	for row in rows:
		if _conversion_note_exists(row.name, row.converted_enrollment):
			continue
		enrollment = _get_enrollment(row.converted_enrollment)
		if not enrollment:
			continue
		invoice = row.get("converted_invoice") or enrollment.get("invoice")
		note = frappe.new_doc("Inquiry Note")
		note.inquiry = row.name
		note.student = row.get("student") or enrollment.get("student")
		note.note = _build_note(enrollment, invoice)
		note.author = frappe.session.user
		note.edited_at = now_datetime()
		_set_if_field(note, "note_type", "System")
		_set_if_field(note, "source_doctype", "Enrollment")
		_set_if_field(note, "source_document", row.converted_enrollment)
		note.flags.ignore_permissions = True
		note.insert()


def _conversion_note_exists(inquiry, enrollment):
	filters = {"inquiry": inquiry}
	if frappe.db.has_column("Inquiry Note", "source_doctype"):
		filters["source_doctype"] = "Enrollment"
	if frappe.db.has_column("Inquiry Note", "source_document"):
		filters["source_document"] = enrollment
	return bool(frappe.db.exists("Inquiry Note", filters))


def _get_enrollment(enrollment):
	fields = ["name", "student", "term", "course", "weekly_timeslot", "start_course_session", "enrollment_date"]
	for fieldname in ("remaining_sessions", "invoice", "invoice_amount"):
		if frappe.db.has_column("Enrollment", fieldname):
			fields.append(fieldname)
	rows = frappe.get_all("Enrollment", filters={"name": enrollment}, fields=fields, limit=1)
	return rows[0] if rows else None


def _build_note(enrollment, invoice):
	parts = [
		_("Trial converted to full-term enrollment."),
		_("Course: {0}").format(enrollment.get("course")),
		_("Term: {0}").format(enrollment.get("term")),
	]
	if enrollment.get("start_course_session"):
		parts.append(_("Start session: {0}").format(enrollment.get("start_course_session")))
	if enrollment.get("enrollment_date"):
		parts.append(_("Start date: {0}").format(enrollment.get("enrollment_date")))
	if enrollment.get("remaining_sessions") is not None:
		parts.append(_("Remaining sessions: {0}").format(enrollment.get("remaining_sessions")))
	parts.append(_("Enrollment: {0}").format(enrollment.get("name")))
	if invoice:
		parts.append(_("Draft invoice: {0}").format(invoice))
		amount = _get_invoice_amount(invoice) or enrollment.get("invoice_amount")
		if amount is not None:
			parts.append(_("Draft invoice amount: {0}").format(flt(amount)))
	return " ".join(str(part) for part in parts if part)


def _get_invoice_amount(invoice):
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		return None
	return frappe.db.get_value("Sales Invoice", invoice, "grand_total")


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)
