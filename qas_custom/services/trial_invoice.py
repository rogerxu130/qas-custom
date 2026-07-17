from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, format_time, formatdate

from qas_custom.modules.billing.commands import get_invoice_customer, get_invoice_item, get_trial_class_fee
from qas_custom.services.display_labels import get_course_session_snapshot_label, get_student_display_code, get_student_parent_name
from qas_custom.services.maintenance import _issue, _make_issue_key, record_data_issue, resolve_data_issue


ELIGIBLE_INQUIRY_STATUSES = {"Booked", "Rescheduled"}
TRIAL_INVOICE_JOB = "qas_custom.services.trial_invoice.create_trial_invoice_job"


def enqueue_trial_invoice_for_inquiry(inquiry_doc):
	status = get_trial_invoice_status(inquiry_doc)
	should_validate_existing = bool(status.get("trial_invoice") and _is_eligible(inquiry_doc))
	if status.get("trial_invoice_status") != "queued" and not should_validate_existing:
		return status
	frappe.enqueue(
		TRIAL_INVOICE_JOB,
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=f"trial-invoice:{inquiry_doc.name}",
		deduplicate=True,
		inquiry=inquiry_doc.name,
	)
	return status


def create_trial_invoice_job(inquiry: str):
	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		return _create_trial_invoice(inquiry)
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), f"QAS Trial Invoice automation failed: {inquiry}")
		_record_trial_invoice_issue(inquiry, str(exc))
		return {
			"trial_invoice": _inquiry_trial_invoice(inquiry),
			"trial_invoice_status": "failed",
			"trial_invoice_message": str(exc),
		}
	finally:
		frappe.set_user(original_user)


def _create_trial_invoice(inquiry: str):
	doc = frappe.get_doc("Inquiry", inquiry)
	if not _is_eligible(doc):
		return _status_payload(doc, "skipped", _("Trial Inquiry is not currently eligible for automatic invoicing."))

	invoice_name = doc.get("trial_invoice") or _find_inquiry_invoice(doc.name)
	if invoice_name:
		_link_invoice(doc.name, invoice_name)
		invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if cint(invoice_doc.docstatus) == 2 or invoice_doc.get("status") == "Cancelled":
			return _status_payload(doc, "skipped", _("The linked Trial Invoice is cancelled and will not be recreated."), invoice=invoice_name)
		if cint(invoice_doc.docstatus) == 1:
			_check_rescheduled_trial_fee(doc, invoice_doc)
			resolve_data_issue(_trial_invoice_issue_key(doc.name))
			frappe.db.commit()
			return _status_payload(doc, "linked", _("Trial Invoice {0} is already submitted.").format(invoice_name), invoice=invoice_name)
	else:
		context = _trial_invoice_context(doc)
		invoice_name = _create_draft_trial_invoice(doc, context)
		_link_invoice(doc.name, invoice_name)
		frappe.db.commit()

	from qas_custom.services.school_admin import submit_school_admin_invoice_data

	result = submit_school_admin_invoice_data(
		invoice=invoice_name,
		enqueue_notification=True,
		send_notifications=True,
	)
	resolve_data_issue(_trial_invoice_issue_key(doc.name))
	frappe.db.commit()
	notification = result.get("notification") or {}
	message = _("Trial Invoice {0} was submitted.").format(invoice_name)
	if notification.get("queued"):
		message = _("Trial Invoice {0} was submitted and the parent notification was queued.").format(invoice_name)
	return _status_payload(doc, "linked", message, invoice=invoice_name)


def get_trial_invoice_status(inquiry_doc):
	doc = frappe.get_doc("Inquiry", inquiry_doc) if isinstance(inquiry_doc, str) else inquiry_doc
	invoice_name = doc.get("trial_invoice") or _find_inquiry_invoice(doc.name)
	issue = _open_trial_invoice_issue(doc.name)
	if invoice_name:
		invoice = frappe.db.get_value("Sales Invoice", invoice_name, ["name", "docstatus", "status"], as_dict=True)
		if not invoice:
			return _status_payload(doc, "failed", _("The linked Trial Invoice could not be found."), invoice=invoice_name)
		if cint(invoice.get("docstatus")) == 2 or invoice.get("status") == "Cancelled":
			return _status_payload(doc, "skipped", _("The linked Trial Invoice is cancelled and will not be recreated."), invoice=invoice_name)
		if issue and cint(invoice.get("docstatus")) == 0:
			return _status_payload(doc, "failed", issue.get("description") or _("Trial Invoice automation requires review."), invoice=invoice_name)
		if cint(invoice.get("docstatus")) == 1:
			if issue:
				return _status_payload(doc, "linked", issue.get("description") or _("Trial Invoice is linked but requires review."), invoice=invoice_name)
			return _status_payload(doc, "linked", _("Trial Invoice {0} is submitted.").format(invoice_name), invoice=invoice_name)
		return _status_payload(doc, "queued", _("Trial Invoice {0} is waiting to be submitted.").format(invoice_name), invoice=invoice_name)
	if issue:
		return _status_payload(doc, "failed", issue.get("description") or _("Trial Invoice automation requires review."))
	if _is_eligible(doc):
		return _status_payload(doc, "queued", _("Trial Invoice creation is queued."))
	return _status_payload(doc, "skipped", _("Trial Invoice is not required until a Trial Lesson is booked."))


def _trial_invoice_context(inquiry_doc):
	session = frappe.db.get_value(
		"Course Sessions",
		inquiry_doc.course_session,
		["name", "weekly_timeslot", "session_date", "status"],
		as_dict=True,
	)
	if not session or not session.get("weekly_timeslot"):
		frappe.throw(_("Booked Course Session or Weekly Timeslot could not be found."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.get("weekly_timeslot"),
		["name", "course", "campus", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot or not timeslot.get("course"):
		frappe.throw(_("Booked Weekly Timeslot is missing its Course."))
	fee = get_trial_class_fee(timeslot.get("course"))
	if fee <= 0:
		frappe.throw(_("Course {0} is missing a positive Trial Class Fee.").format(timeslot.get("course")))
	return {
		"session": session,
		"timeslot": timeslot,
		"course": timeslot.get("course"),
		"campus": timeslot.get("campus") or inquiry_doc.get("campus"),
		"fee": fee,
		"customer": get_invoice_customer(inquiry_doc.parent),
		"item_code": get_invoice_item(timeslot.get("course")),
	}


def _create_draft_trial_invoice(inquiry_doc, context):
	from qas_custom.services.school_admin import create_school_admin_manual_invoice_data

	student_name = get_student_parent_name(inquiry_doc.student) or inquiry_doc.student
	student_code = get_student_display_code(inquiry_doc.student) or inquiry_doc.student
	session = context["session"]
	timeslot = context["timeslot"]
	description = _("{0} - Trial Lesson - {1} - {2} {3}-{4} - {5}").format(
		student_name,
		context["course"],
		formatdate(session.get("session_date")),
		format_time(timeslot.get("start_time")),
		format_time(timeslot.get("end_time")),
		context.get("campus") or _("Campus not set"),
	)
	payload = create_school_admin_manual_invoice_data({
		"customer": context["customer"],
		"parent": inquiry_doc.parent,
		"student": inquiry_doc.student,
		"course": context["course"],
		"qas_invoice_type": "Other",
		"source_doctype": "Inquiry",
		"source_document": inquiry_doc.name,
		"source_type": "Trial Inquiry",
		"billing_note": _("Trial Invoice generated automatically from Inquiry {0}.").format(inquiry_doc.name),
		"remarks": _("Automatically generated Trial Invoice for Inquiry {0}.").format(inquiry_doc.name),
		"items": [{
			"item_code": context["item_code"],
			"item_name": _("Trial Lesson - {0}").format(context["course"]),
			"description": description,
			"qty": 1,
			"rate": flt(context["fee"]),
			"qas_line_type": "Course Fee",
			"student": inquiry_doc.student,
			"student_display_name": student_name,
			"student_code": student_code,
			"course": context["course"],
			"course_session": get_course_session_snapshot_label(inquiry_doc.course_session),
			"session_count": 1,
		}],
	})
	return payload.get("name")


def _find_inquiry_invoice(inquiry: str):
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return None
	return frappe.db.get_value(
		"Sales Invoice",
		{"source_doctype": "Inquiry", "source_document": inquiry},
		"name",
		order_by="creation asc",
	)


def _link_invoice(inquiry: str, invoice: str):
	if inquiry and invoice and frappe.db.get_value("Inquiry", inquiry, "trial_invoice") != invoice:
		frappe.db.set_value("Inquiry", inquiry, "trial_invoice", invoice, update_modified=True)


def _record_trial_invoice_issue(inquiry: str, message: str):
	if not inquiry or not frappe.db.exists("Inquiry", inquiry):
		return None
	doc = frappe.get_doc("Inquiry", inquiry)
	return record_data_issue(_issue(
		key_parts=["trial-invoice", inquiry],
		issue_type="Billing Configuration",
		severity="Warning",
		source_doctype="Inquiry",
		source_document=inquiry,
		related_doctype="Sales Invoice" if doc.get("trial_invoice") else None,
		related_document=doc.get("trial_invoice"),
		student=doc.get("student"),
		course_session=doc.get("course_session"),
		description=_("Automatic Trial Invoice could not be completed: {0}").format(message),
		suggested_action=_("Correct the Course Trial Class Fee or linked billing data, then retry the Trial Invoice job."),
	))


def _check_rescheduled_trial_fee(inquiry_doc, invoice_doc):
	current_fee, course = _current_booking_fee(inquiry_doc)
	invoiced_fee = sum(flt(row.get("amount") or (flt(row.get("qty") or 1) * flt(row.get("rate")))) for row in invoice_doc.get("items") or [])
	issue_key = _reschedule_fee_issue_key(inquiry_doc.name)
	if current_fee > 0 and abs(current_fee - invoiced_fee) <= 0.005:
		resolve_data_issue(issue_key)
		return None
	return record_data_issue(_issue(
		key_parts=["trial-invoice-fee-change", inquiry_doc.name],
		issue_type="Billing Configuration",
		severity="Warning",
		source_doctype="Inquiry",
		source_document=inquiry_doc.name,
		related_doctype="Sales Invoice",
		related_document=invoice_doc.name,
		student=inquiry_doc.get("student"),
		course_session=inquiry_doc.get("course_session"),
		description=_("Rescheduled Trial Inquiry now uses Course {0} with Trial Class Fee {1}, but linked Invoice {2} totals {3}.").format(
			course or _("Unknown"), current_fee, invoice_doc.name, invoiced_fee
		),
		suggested_action=_("Review the linked Invoice and manually decide whether a cancellation, replacement, or price adjustment is required."),
	))


def _current_booking_fee(inquiry_doc):
	session = frappe.db.get_value("Course Sessions", inquiry_doc.get("course_session"), "weekly_timeslot")
	course = frappe.db.get_value("Weekly Timeslot", session, "course") if session else None
	return get_trial_class_fee(course), course


def _open_trial_invoice_issue(inquiry: str):
	if not frappe.db.exists("DocType", "QAS Data Issue"):
		return None
	return frappe.db.get_value(
		"QAS Data Issue",
		{"issue_key": ["in", [_trial_invoice_issue_key(inquiry), _reschedule_fee_issue_key(inquiry)]], "status": "Open"},
		["name", "description"],
		as_dict=True,
	)


def _trial_invoice_issue_key(inquiry: str):
	return _make_issue_key(["trial-invoice", inquiry])


def _reschedule_fee_issue_key(inquiry: str):
	return _make_issue_key(["trial-invoice-fee-change", inquiry])


def _inquiry_trial_invoice(inquiry: str):
	return frappe.db.get_value("Inquiry", inquiry, "trial_invoice") if inquiry else None


def _is_eligible(inquiry_doc):
	return bool(
		inquiry_doc
		and inquiry_doc.get("inquiry_type") == "Trial Lesson"
		and inquiry_doc.get("status") in ELIGIBLE_INQUIRY_STATUSES
		and inquiry_doc.get("course_session")
	)


def _status_payload(inquiry_doc, status, message, invoice=None):
	return {
		"trial_invoice": invoice or inquiry_doc.get("trial_invoice"),
		"trial_invoice_status": status,
		"trial_invoice_message": message,
	}
