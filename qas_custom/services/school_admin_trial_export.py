from __future__ import annotations

from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate
from frappe.utils.xlsxutils import make_xlsx

from qas_custom.services.inquiry import _submission_data_rows
from qas_custom.services.school_admin import _has_field, _require_school_admin, _safe_fields
from qas_custom.services.trial_invoice import get_trial_invoice_status


EXPORT_FIXED_COLUMNS = [
	("inquiry_id", "Inquiry ID"),
	("submitted_at", "Submit time"),
	("inquiry_status", "Inquiry status"),
	("parent_name", "Parent name"),
	("parent_phone", "Parent phone"),
	("parent_email", "Parent email"),
	("student_name", "Student name"),
	("student_date_of_birth", "Student date of birth"),
	("campus", "Campus"),
	("course", "Course"),
	("trial_appointment_date", "Trial appointment date"),
	("trial_appointment_time", "Trial appointment time"),
	("course_session", "Course Session"),
	("trial_invoice", "Trial Invoice"),
	("invoice_amount", "Invoice amount"),
	("invoice_status", "Invoice status"),
	("invoice_paid", "Invoice paid"),
	("converted", "Converted"),
	("converted_enrollment", "Converted Enrollment"),
	("converted_time", "Converted time"),
	("source", "Source"),
	("submission_id", "Submission ID"),
	("form_id", "Form ID"),
	("serial", "Serial"),
	("source_url", "Source URL"),
]

SUBMISSION_METADATA_LABELS = {
	"source",
	"submission id",
	"form id",
	"serial",
	"serial number",
	"submitted at",
	"created at",
	"updated at",
	"source url",
	"formname",
	"form name",
	"inquiry type",
}


def export_school_admin_trial_inquiries_data(from_date=None, to_date=None):
	_require_school_admin()
	from_date, to_date = _validate_export_dates(from_date, to_date)
	rows = _get_export_inquiries(from_date, to_date)
	if not rows:
		frappe.local.response.http_status_code = 204
		return None

	data_rows = _build_export_rows(rows)
	if not data_rows:
		frappe.local.response.http_status_code = 204
		return None

	headers = [label for _key, label in EXPORT_FIXED_COLUMNS]
	dynamic_labels = _dynamic_form_labels(data_rows)
	data = [headers + dynamic_labels]
	data.extend(_export_row_values(row, dynamic_labels) for row in data_rows)

	column_widths = [
		18, 20, 18, 24, 18, 30, 24, 18, 20, 24, 18, 14, 22,
		18, 14, 18, 14, 12, 22, 20, 18, 18, 22, 16, 40,
	]
	column_widths.extend(max(18, min(42, len(label) + 4)) for label in dynamic_labels)
	xlsx_file = make_xlsx(data, "Trial Inquiries", column_widths=column_widths)
	filename = f"trial-inquiries-{from_date}-to-{to_date}.xlsx"
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = xlsx_file.getvalue()
	frappe.local.response.content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
	frappe.local.response.display_content_as = "attachment"
	frappe.local.response.type = "download"
	return None


def _validate_export_dates(from_date, to_date):
	from_text = str(from_date or "").strip()
	to_text = str(to_date or "").strip()
	if not from_text or not to_text:
		frappe.throw(_("Both Submit dates are required."))
	try:
		start = getdate(from_text)
		end = getdate(to_text)
	except Exception:
		frappe.throw(_("Submit dates must use YYYY-MM-DD format."))
	if not start or not end:
		frappe.throw(_("Submit dates must use YYYY-MM-DD format."))
	if start > end:
		frappe.throw(_("Submit from date cannot be later than Submit to date."))
	return start.isoformat(), end.isoformat()


def _get_export_inquiries(from_date, to_date):
	if not _has_field("Inquiry", "external_submitted_at"):
		frappe.throw(_("Original Submit time is not available on Inquiry."))
	fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"inquiry_type",
			"status",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"submitted_student_name",
			"submitted_student_dob",
			"campus",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
			"trial_invoice",
			"converted_enrollment",
			"converted_invoice",
			"external_submitted_at",
			"external_submission_id",
			"external_form_id",
			"external_serial_number",
			"source",
			"webhook_source",
			"source_url",
			"raw_webhook_payload",
			"modified",
		],
	)
	return frappe.get_all(
		"Inquiry",
		filters={
			"inquiry_type": "Trial Lesson",
			"external_submitted_at": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]],
		},
		fields=fields,
		order_by="external_submitted_at asc, name asc",
		limit_page_length=0,
	)


def _build_export_rows(rows):
	parent_map = _parent_map(rows)
	student_map = _student_map(rows)
	conversion_time_map = _conversion_time_map(rows)
	export_rows = []

	for inquiry in rows:
		invoice_status = get_trial_invoice_status(inquiry)
		invoice_name = invoice_status.get("trial_invoice") or inquiry.get("trial_invoice") or ""
		invoice = _invoice_summary(invoice_name)
		invoice_paid = _invoice_is_paid(invoice)
		converted = inquiry.get("status") == "Converted" or bool(inquiry.get("converted_enrollment"))
		if not _is_export_eligible(invoice_paid=invoice_paid, converted=converted):
			continue

		parent = parent_map.get(inquiry.get("parent")) or {}
		student = student_map.get(inquiry.get("student")) or {}
		export_rows.append(
			{
				"inquiry_id": inquiry.get("name"),
				"submitted_at": inquiry.get("external_submitted_at"),
				"inquiry_status": inquiry.get("status"),
				"parent_name": parent.get("parent_name") or inquiry.get("contact_name") or inquiry.get("parent"),
				"parent_phone": inquiry.get("contact_phone"),
				"parent_email": inquiry.get("contact_email"),
				"student_name": student.get("student_name") or inquiry.get("submitted_student_name") or inquiry.get("student"),
				"student_date_of_birth": student.get("date_of_birth") or student.get("dob") or inquiry.get("submitted_student_dob"),
				"campus": inquiry.get("campus"),
				"course": inquiry.get("preferred_course"),
				"trial_appointment_date": inquiry.get("current_appointment_date"),
				"trial_appointment_time": inquiry.get("current_appointment_time"),
				"course_session": inquiry.get("course_session"),
				"trial_invoice": invoice_name,
				"invoice_amount": invoice.get("grand_total"),
				"invoice_status": invoice.get("status") or invoice_status.get("trial_invoice_status"),
				"invoice_paid": "Yes" if invoice_paid else "No",
				"converted": "Yes" if converted else "No",
				"converted_enrollment": inquiry.get("converted_enrollment"),
				"converted_time": conversion_time_map.get(inquiry.get("name")),
				"source": inquiry.get("source") or inquiry.get("webhook_source"),
				"submission_id": inquiry.get("external_submission_id"),
				"form_id": inquiry.get("external_form_id"),
				"serial": inquiry.get("external_serial_number"),
				"source_url": inquiry.get("source_url"),
				"form_answers": _form_answers(inquiry.get("raw_webhook_payload")),
			}
		)
	return export_rows


def _parent_map(rows):
	parents = {row.get("parent") for row in rows if row.get("parent")}
	if not parents or not frappe.db.exists("DocType", "Parent"):
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Parent",
			filters={"name": ["in", list(parents)]},
			fields=_safe_fields("Parent", ["name", "parent_name"]),
			limit_page_length=0,
		)
	}


def _student_map(rows):
	students = {row.get("student") for row in rows if row.get("student")}
	if not students or not frappe.db.exists("DocType", "Student"):
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Student",
			filters={"name": ["in", list(students)]},
			fields=_safe_fields("Student", ["name", "student_name", "date_of_birth", "dob"]),
			limit_page_length=0,
		)
	}


def _invoice_summary(invoice_name):
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return {}
	return frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		_safe_fields("Sales Invoice", ["name", "docstatus", "status", "grand_total", "outstanding_amount"]),
		as_dict=True,
	) or {}


def _invoice_is_paid(invoice):
	return bool(
		invoice
		and cint(invoice.get("docstatus")) == 1
		and invoice.get("status") != "Cancelled"
		and (invoice.get("status") == "Paid" or flt(invoice.get("outstanding_amount")) <= 0.005)
	)


def _is_export_eligible(*, invoice_paid, converted):
	return bool(invoice_paid or converted)


def _conversion_time_map(rows):
	inquiry_names = [row.get("name") for row in rows if row.get("name") and (row.get("status") == "Converted" or row.get("converted_enrollment"))]
	if not inquiry_names or not frappe.db.exists("DocType", "Inquiry Note"):
		return {}
	fields = _safe_fields("Inquiry Note", ["inquiry", "creation", "edited_at", "source_doctype", "source_document"])
	if "source_doctype" not in fields:
		return {}
	filters = {"inquiry": ["in", inquiry_names]}
	filters["source_doctype"] = "Enrollment"
	notes = frappe.get_all("Inquiry Note", filters=filters, fields=fields, order_by="creation asc", limit_page_length=0)
	return {note.inquiry: note.get("creation") or note.get("edited_at") for note in notes if note.get("inquiry")}


def _form_answers(raw_payload):
	answers = OrderedDict()
	for item in _submission_data_rows(raw_payload):
		label = str(item.get("label") or "").strip()
		if not label or _normalise_label(label) in SUBMISSION_METADATA_LABELS:
			continue
		value = _safe_export_value(item.get("value"))
		answers.setdefault(label, []).append(value)
	return {label: "; ".join(values) for label, values in answers.items()}


def _dynamic_form_labels(rows):
	labels = []
	seen = set()
	for row in rows:
		for label in row.get("form_answers", {}):
			if label not in seen:
				seen.add(label)
				labels.append(label)
	return labels


def _export_row_values(row, dynamic_labels):
	values = [_safe_export_value(row.get(key)) for key, _label in EXPORT_FIXED_COLUMNS]
	values.extend(_safe_export_value(row.get("form_answers", {}).get(label, "")) for label in dynamic_labels)
	return values


def _normalise_label(value):
	return " ".join(str(value or "").replace("_", " ").replace("-", " ").split()).lower()


def _safe_export_value(value):
	if value is None:
		return ""
	if isinstance(value, bool):
		return "Yes" if value else "No"
	if isinstance(value, str):
		value = value.replace("\x00", "").strip()
		if value.startswith(("=", "+", "-", "@")):
			return "'" + value
	return value
