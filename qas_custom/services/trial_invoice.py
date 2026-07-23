from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, format_time, formatdate

from qas_custom.modules.billing.commands import get_invoice_customer, get_invoice_item, get_trial_class_fee
from qas_custom.services.display_labels import get_course_session_snapshot_label, get_student_display_code, get_student_parent_name
from qas_custom.services.maintenance import _issue, _make_issue_key, record_data_issue, resolve_data_issue


ELIGIBLE_INQUIRY_STATUSES = {"Booked", "Rescheduled"}
TRIAL_INVOICE_JOB = "qas_custom.services.trial_invoice.create_trial_invoice_job"
REPLACEMENT_TRIAL_INVOICE_LOCK_PREFIX = "qas-replacement-trial-invoice"


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
		if invoice_doc.get("source_type") == "Replacement Trial Inquiry":
			return _status_payload(
				doc,
				"queued",
				_("Replacement Trial Invoice {0} is waiting for School Admin review and submission.").format(invoice_name),
				invoice=invoice_name,
			)
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


def preview_replacement_trial_invoice(inquiry: str):
	doc = _replacement_inquiry_doc(inquiry)
	classification = _classify_replacement_invoices(doc, _inquiry_invoice_rows(doc))
	payload = {
		"state": classification["state"],
		"can_create": classification["state"] == "ready",
		"message": classification["message"],
		"current_invoice": _replacement_invoice_summary(classification.get("invoice")),
		"replacement": None,
	}
	if classification["state"] == "existing_draft":
		payload["existing_draft"] = payload["current_invoice"]
		return payload
	if classification["state"] != "ready":
		return payload

	context = _replacement_trial_invoice_context(doc)
	payload["replacement"] = _replacement_booking_payload(doc, context)
	return payload


def create_replacement_trial_invoice_draft(inquiry: str):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	lock_name = "{0}:{1}".format(REPLACEMENT_TRIAL_INVOICE_LOCK_PREFIX, inquiry)
	with frappe.cache.lock(lock_name, timeout=60, blocking_timeout=10):
		return _create_replacement_trial_invoice_draft(inquiry)


def _create_replacement_trial_invoice_draft(inquiry: str):
	preview = preview_replacement_trial_invoice(inquiry)
	if preview["state"] == "blocked":
		frappe.throw(_(preview["message"]))
	if preview["state"] == "existing_draft":
		invoice = preview.get("existing_draft") or {}
		invoice_name = invoice.get("name")
		if invoice_name:
			_link_invoice(inquiry, invoice_name)
			frappe.db.commit()
		return {
			"created": False,
			"invoice": invoice,
			"message": _("Existing Draft Invoice {0} was reused.").format(invoice_name),
		}
		frappe.throw(_("The existing replacement Draft Invoice could not be found."))

	doc = _replacement_inquiry_doc(inquiry)
	context = _replacement_trial_invoice_context(doc)
	savepoint = "replacement_trial_invoice"
	frappe.db.savepoint(savepoint)
	try:
		# Repeat the classification inside the lock and transaction immediately
		# before creating a financial document.
		classification = _classify_replacement_invoices(doc, _inquiry_invoice_rows(doc))
		if classification["state"] == "blocked":
			frappe.throw(_(classification["message"]))
		if classification["state"] == "existing_draft":
			existing = _replacement_invoice_summary(classification.get("invoice"))
			_link_invoice(doc.name, existing.get("name"))
			frappe.db.commit()
			return {
				"created": False,
				"invoice": existing,
				"message": _("Existing Draft Invoice {0} was reused.").format(existing.get("name")),
			}

		old_invoice = classification.get("invoice")
		invoice_name = _create_draft_trial_invoice(doc, context, replacement=True)
		_link_invoice(doc.name, invoice_name)
		_record_replacement_trial_invoice_audit(doc, old_invoice, invoice_name)
		resolve_data_issue(_trial_invoice_issue_key(doc.name))
		resolve_data_issue(_reschedule_fee_issue_key(doc.name))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	invoice = frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		["name", "docstatus", "status", "grand_total", "rounded_total", "outstanding_amount", "creation"],
		as_dict=True,
	)
	return {
		"created": True,
		"invoice": _replacement_invoice_summary(invoice),
		"message": _("Replacement Trial Invoice {0} was created as a Draft. No parent email was sent.").format(invoice_name),
	}


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


def _create_draft_trial_invoice(inquiry_doc, context, replacement=False):
	from qas_custom.services.school_admin import _add_comment, _create_school_admin_manual_invoice_doc

	payload = _trial_invoice_draft_payload(inquiry_doc, context, replacement=replacement)
	invoice = _create_school_admin_manual_invoice_doc(payload)
	_add_comment(
		"Sales Invoice",
		invoice.name,
		_("Replacement Trial Invoice created from Inquiry {0}.").format(inquiry_doc.name)
		if replacement
		else _("Trial Invoice generated automatically from Inquiry {0}.").format(inquiry_doc.name),
	)
	return invoice.name


def _trial_invoice_draft_payload(inquiry_doc, context, replacement=False):
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
	return {
		"customer": context["customer"],
		"parent": inquiry_doc.parent,
		"student": inquiry_doc.student,
		"course": context["course"],
		"qas_invoice_type": "Other",
		"source_doctype": "Inquiry",
		"source_document": inquiry_doc.name,
		"source_type": "Replacement Trial Inquiry" if replacement else "Trial Inquiry",
		"billing_note": (
			_("Replacement Trial Invoice generated from Inquiry {0}.").format(inquiry_doc.name)
			if replacement
			else _("Trial Invoice generated automatically from Inquiry {0}.").format(inquiry_doc.name)
		),
		"remarks": (
			_("Replacement Trial Invoice generated from Inquiry {0}.").format(inquiry_doc.name)
			if replacement
			else _("Automatically generated Trial Invoice for Inquiry {0}.").format(inquiry_doc.name)
		),
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
	}


def _replacement_inquiry_doc(inquiry: str):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	doc = frappe.get_doc("Inquiry", inquiry)
	if not _is_eligible(doc):
		frappe.throw(_("Only a Booked or Rescheduled Trial Lesson with a Course Session can generate a replacement Invoice."))
	if not doc.get("parent"):
		frappe.throw(_("The Inquiry is missing its Parent."))
	if not doc.get("student"):
		frappe.throw(_("The Inquiry is missing its Student."))
	return doc


def _replacement_trial_invoice_context(inquiry_doc):
	context = _trial_invoice_context(inquiry_doc)
	if not context.get("campus"):
		frappe.throw(_("The booked Course Session is missing its Campus."))
	return context


def _replacement_booking_payload(inquiry_doc, context):
	session = context["session"]
	timeslot = context["timeslot"]
	return {
		"inquiry": inquiry_doc.name,
		"student": inquiry_doc.student,
		"student_name": get_student_parent_name(inquiry_doc.student) or inquiry_doc.student,
		"course": context["course"],
		"course_session": inquiry_doc.course_session,
		"session_label": get_course_session_snapshot_label(inquiry_doc.course_session),
		"session_date": session.get("session_date"),
		"start_time": timeslot.get("start_time"),
		"end_time": timeslot.get("end_time"),
		"campus": context.get("campus"),
		"trial_fee": flt(context["fee"]),
	}


def _inquiry_invoice_rows(inquiry_doc):
	fields = ["name", "docstatus", "status", "grand_total", "rounded_total", "outstanding_amount", "creation"]
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"source_doctype": "Inquiry", "source_document": inquiry_doc.name},
		fields=fields,
		order_by="creation desc",
		limit_page_length=0,
	)
	names = {row.get("name") for row in rows}
	linked_invoice = inquiry_doc.get("trial_invoice")
	if linked_invoice and linked_invoice not in names:
		linked = frappe.db.get_value("Sales Invoice", linked_invoice, fields, as_dict=True)
		if linked:
			rows.insert(0, linked)
	return rows


def _classify_replacement_invoices(inquiry_doc, invoice_rows):
	rows = [frappe._dict(row) for row in invoice_rows if row and row.get("name")]
	linked_invoice = inquiry_doc.get("trial_invoice")
	rows.sort(key=lambda row: str(row.get("creation") or ""), reverse=True)
	if linked_invoice:
		rows.sort(key=lambda row: row.get("name") != linked_invoice)

	active_submitted = [row for row in rows if cint(row.get("docstatus")) == 1 and not _invoice_is_cancelled(row)]
	if active_submitted:
		invoice = _preferred_invoice(active_submitted, linked_invoice)
		return {
			"state": "blocked",
			"invoice": invoice,
			"message": _(
				"Invoice {0} is still active with status {1}. Cancel it manually before generating a replacement."
			).format(invoice.get("name"), invoice.get("status") or _("Submitted")),
		}

	drafts = [row for row in rows if cint(row.get("docstatus")) == 0 and not _invoice_is_cancelled(row)]
	if len(drafts) > 1:
		return {
			"state": "blocked",
			"invoice": _preferred_invoice(drafts, linked_invoice),
			"message": _("Multiple Draft Invoices already exist for this Inquiry. Review them before continuing."),
		}
	if drafts:
		invoice = drafts[0]
		return {
			"state": "existing_draft",
			"invoice": invoice,
			"message": _("Draft Invoice {0} already exists for this Inquiry.").format(invoice.get("name")),
		}

	cancelled = [row for row in rows if _invoice_is_cancelled(row)]
	if not cancelled:
		return {
			"state": "blocked",
			"invoice": None,
			"message": _("No cancelled Trial Invoice was found. This action only creates a replacement after manual cancellation."),
		}
	invoice = _preferred_invoice(cancelled, linked_invoice)
	return {
		"state": "ready",
		"invoice": invoice,
		"message": _("Cancelled Invoice {0} can be replaced with a new Draft.").format(invoice.get("name")),
	}


def _preferred_invoice(rows, linked_invoice):
	return next((row for row in rows if row.get("name") == linked_invoice), rows[0])


def _invoice_is_cancelled(invoice):
	return cint(invoice.get("docstatus")) == 2 or str(invoice.get("status") or "").lower() == "cancelled"


def _replacement_invoice_summary(invoice):
	if not invoice:
		return None
	docstatus = cint(invoice.get("docstatus"))
	status = "Cancelled" if _invoice_is_cancelled(invoice) else invoice.get("status") or ("Draft" if docstatus == 0 else "Submitted")
	amount = flt(invoice.get("rounded_total") or invoice.get("grand_total") or 0)
	return {
		"name": invoice.get("name"),
		"docstatus": docstatus,
		"status": status,
		"amount": amount,
		"outstanding_amount": flt(invoice.get("outstanding_amount") or 0),
	}


def _record_replacement_trial_invoice_audit(inquiry_doc, old_invoice, new_invoice):
	from qas_custom.services.school_admin import _add_comment

	old_invoice_name = old_invoice.get("name") if old_invoice else None
	_add_comment(
		"Inquiry",
		inquiry_doc.name,
		_("Replacement Trial Invoice {0} created after cancelled Invoice {1}.").format(
			new_invoice, old_invoice_name or _("not linked")
		),
	)
	if old_invoice_name:
		_add_comment(
			"Sales Invoice",
			old_invoice_name,
			_("Replacement Draft Invoice {0} was created from Inquiry {1}.").format(new_invoice, inquiry_doc.name),
		)


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
