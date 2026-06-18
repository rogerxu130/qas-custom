from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate, today

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.services.inquiry import (
	add_inquiry_note_core,
	build_inquiry_detail,
	build_inquiry_summary,
	mark_inquiry_status_core,
	reschedule_inquiry_core,
)


ADMIN_ROLES = {"School Admin", "System Manager"}
INQUIRY_OPEN_STATUSES = ["New", "Needs Review", "Booked", "Rescheduled", "No-show"]
INQUIRY_POST_VISIT_STATUSES = ["Completed", "Follow-up"]


def get_school_admin_me_data():
	_require_school_admin()
	return {
		"user": frappe.session.user,
		"roles": sorted(set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES)),
		"active": True,
	}


def get_school_admin_csrf_token_data():
	_require_school_admin()
	return {"csrf_token": frappe.sessions.get_csrf_token()}


def get_school_admin_dashboard_data():
	_require_school_admin()
	start_date = getdate(today())
	end_date = getdate(add_days(start_date, 7))
	return {
		"date": str(start_date),
		"action_counts": {
			"draft_invoices": _count_sales_invoices({"docstatus": 0}),
			"trial_needs_scheduling": _count(
				"Inquiry",
				{"inquiry_type": "Trial Lesson", "status": "Needs Review"},
			),
			"school_visit_needs_review": _count(
				"Inquiry",
				{"inquiry_type": "School Visit", "status": "Needs Review"},
			),
			"post_visit_follow_up": _count(
				"Inquiry",
				{"status": ["in", INQUIRY_POST_VISIT_STATUSES]},
			),
			"active_enrollments": _count("Enrollment", {"status": "Active"}),
		},
		"upcoming": {
			"from_date": str(start_date),
			"to_date": str(end_date),
			"trial_lessons": _count(
				"Inquiry",
				{
					"inquiry_type": "Trial Lesson",
					"status": ["in", ["Booked", "Rescheduled"]],
					"current_appointment_date": ["between", [start_date, end_date]],
				},
			),
			"school_visits": _count(
				"Inquiry",
				{
					"inquiry_type": "School Visit",
					"status": ["in", ["Booked", "Rescheduled"]],
					"current_appointment_date": ["between", [start_date, end_date]],
				},
			),
			"course_sessions": _count(
				"Course Sessions",
				{"session_date": ["between", [start_date, end_date]]},
			),
		},
		"financial": {
			"submitted_invoices": _count_sales_invoices({"docstatus": 1}),
			"cancelled_invoices": _count_sales_invoices({"docstatus": 2}),
		},
	}


def school_admin_global_search_data(query=None, limit=20):
	_require_school_admin()
	query = (query or "").strip()
	if len(query) < 2:
		frappe.throw(_("Search query must be at least 2 characters."))
	limit = _limit(limit, default=20, max_value=50)
	return {
		"query": query,
		"families": _search_parents(query, limit),
		"students": _search_students(query, limit),
		"customers": _search_customers(query, limit),
		"inquiries": _search_inquiries(query, limit),
		"enrollments": _search_enrollments(query, limit),
		"invoices": _search_invoices(query, limit),
	}


def get_school_admin_family_data(parent=None, student=None, customer=None, email=None):
	_require_school_admin()
	context = _resolve_family_context(parent=parent, student=student, customer=customer, email=email)
	if not context.get("parent") and not context.get("student") and not context.get("customer"):
		frappe.throw(_("Family was not found."))

	parent_id = context.get("parent")
	student_id = context.get("student")
	customer_id = context.get("customer")
	students = _get_family_students(parent_id, student_id)
	student_ids = [row.get("name") for row in students if row.get("name")]
	return {
		"parent": _get_parent_payload(parent_id) if parent_id else None,
		"customer": _get_customer_payload(customer_id) if customer_id else None,
		"students": students,
		"enrollments": _get_enrollment_rows(parent=parent_id, students=student_ids, limit=80),
		"inquiries": _get_family_inquiry_rows(parent=parent_id, students=student_ids, email=email, limit=80),
		"invoices": _get_invoice_rows(customer=customer_id, parent=parent_id, students=student_ids, limit=80),
	}


def get_school_admin_inquiries_data(
	status=None,
	inquiry_type=None,
	campus=None,
	from_date=None,
	to_date=None,
	queue=None,
	limit=80,
):
	_require_school_admin()
	filters = {}
	if status:
		filters["status"] = status
	elif queue == "post_visit":
		filters["status"] = ["in", INQUIRY_POST_VISIT_STATUSES]
	elif queue == "upcoming":
		filters["status"] = ["in", INQUIRY_OPEN_STATUSES]
	elif queue == "needs_scheduling":
		filters["status"] = "Needs Review"
	if inquiry_type:
		filters["inquiry_type"] = inquiry_type
	if campus:
		filters["campus"] = campus
	if from_date and to_date:
		filters["current_appointment_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["current_appointment_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["current_appointment_date"] = ["<=", getdate(to_date)]

	fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"inquiry_type",
			"status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
			"converted_enrollment",
			"converted_invoice",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		fields=fields,
		order_by=_inquiry_order_by(queue),
		limit=_limit(limit, default=80, max_value=200),
	)
	return {"items": [_build_inquiry_list_item(row) for row in rows]}


def get_school_admin_inquiry_data(inquiry=None):
	_require_school_admin()
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	return build_inquiry_detail(inquiry)


def add_school_admin_inquiry_note_data(inquiry=None, note=None):
	_require_school_admin()
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)


def update_school_admin_inquiry_status_data(inquiry=None, status=None):
	_require_school_admin()
	status = (status or "").strip()
	if status not in {"Cancelled", "Completed", "No-show", "Follow-up"}:
		frappe.throw(_("Unsupported inquiry status."))
	return mark_inquiry_status_core(inquiry, status, actor=frappe.session.user)


def mark_school_admin_inquiry_completed_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="Completed")


def mark_school_admin_inquiry_no_show_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="No-show")


def mark_school_admin_inquiry_follow_up_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="Follow-up")


def mark_school_admin_inquiry_inactive_data(inquiry=None, inactive_reason=None):
	_require_school_admin()
	return mark_inquiry_inactive_core(inquiry, inactive_reason, actor=frappe.session.user)


def reschedule_school_admin_inquiry_data(inquiry=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	return reschedule_inquiry_core(inquiry, payload, actor=frappe.session.user)


def get_school_admin_conversion_sessions_data(inquiry=None, start_date=None, course=None, campus=None):
	_require_school_admin()
	return get_conversion_session_options(
		inquiry=inquiry,
		start_date=start_date,
		course=course,
		campus=campus,
	)


def convert_school_admin_inquiry_data(inquiry=None, course_session=None):
	_require_school_admin()
	return convert_inquiry_to_full_term_core(inquiry, course_session, actor=frappe.session.user)


def get_school_admin_invoices_data(status=None, customer=None, parent=None, student=None, source=None, limit=80):
	_require_school_admin()
	return {
		"items": _get_invoice_rows(
			status=status,
			customer=customer,
			parent=parent,
			students=[student] if student else None,
			source=source,
			limit=_limit(limit, default=80, max_value=200),
		)
	}


def get_school_admin_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	return _build_invoice_payload(doc)


def create_school_admin_manual_invoice_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	customer = payload.get("customer")
	items = payload.get("items") or []
	if not customer:
		frappe.throw(_("Customer is required."))
	if not items:
		frappe.throw(_("At least one invoice item is required."))

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	invoice.due_date = payload.get("due_date") or nowdate()
	_set_if_field(invoice, "parent", payload.get("parent"))
	_set_if_field(invoice, "student", payload.get("student"))
	_set_if_field(invoice, "enrollment", payload.get("enrollment"))
	_set_if_field(invoice, "course", payload.get("course"))
	_set_if_field(invoice, "source_type", payload.get("source_type") or "Manual")
	_set_if_field(invoice, "remarks", payload.get("remarks"))
	_apply_invoice_items(invoice, items)
	invoice.insert(ignore_permissions=True)
	_add_comment("Sales Invoice", invoice.name, "Manual invoice created by School Admin.")
	frappe.db.commit()
	return _build_invoice_payload(invoice)


def update_school_admin_draft_invoice_data(invoice=None, payload=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be edited."))

	for fieldname in ["customer", "due_date", "remarks"]:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname))
	for fieldname in ["parent", "student", "enrollment", "course", "term", "source_inquiry", "source_type"]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if "items" in payload:
		_apply_invoice_items(doc, payload.get("items") or [])
	doc.save(ignore_permissions=True)
	_add_comment("Sales Invoice", doc.name, "Draft invoice updated by School Admin.")
	frappe.db.commit()
	return _build_invoice_payload(doc)


def submit_school_admin_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be submitted."))
	doc.flags.ignore_permissions = True
	doc.submit()
	_add_comment("Sales Invoice", doc.name, "Invoice approved and submitted by School Admin.")
	frappe.db.commit()
	return _build_invoice_payload(doc)


def cancel_school_admin_invoice_data(invoice=None, reason=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Cancellation reason is required."))

	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) == 2:
		return _build_invoice_payload(doc)
	if cint(doc.docstatus) == 1:
		doc.flags.ignore_permissions = True
		doc.cancel()
		_add_comment("Sales Invoice", doc.name, f"Invoice cancelled by School Admin. Reason: {reason}")
		frappe.db.commit()
		return _build_invoice_payload(doc)

	_mark_draft_invoice_cancelled(doc, reason)
	frappe.db.commit()
	return _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))


def get_school_admin_enrollments_data(
	student=None,
	parent=None,
	course=None,
	term=None,
	enrollment_type=None,
	status=None,
	limit=80,
):
	_require_school_admin()
	filters = {}
	for fieldname, value in {
		"student": student,
		"parent": parent,
		"course": course,
		"term": term,
		"enrollment_type": enrollment_type,
		"status": status,
	}.items():
		if value:
			filters[fieldname] = value
	return {"items": _get_enrollment_rows(filters=filters, limit=_limit(limit, default=80, max_value=200))}


def get_school_admin_enrollment_data(enrollment=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	doc = frappe.get_doc("Enrollment", enrollment)
	return _build_enrollment_payload(doc)


def get_school_admin_weekly_timeslots_data(term=None, course=None, campus=None, teacher=None, status=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Weekly Timeslot"):
		return {"items": []}
	filters = {}
	for fieldname, value in {
		"term": term,
		"course": course,
		"campus": campus,
		"teacher": teacher,
		"status": status,
	}.items():
		if value and _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = value
	fields = _safe_fields(
		"Weekly Timeslot",
		[
			"name",
			"term",
			"course",
			"campus",
			"classroom",
			"teacher",
			"day_of_week",
			"start_time",
			"end_time",
			"status",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters=filters,
		fields=fields,
		order_by="term desc, course asc, campus asc, day_of_week asc, start_time asc",
		limit=_limit(limit, default=120, max_value=300),
	)
	return {"items": [_docdict(row) for row in rows]}


def get_school_admin_weekly_timeslot_data(weekly_timeslot=None):
	_require_school_admin()
	if not _doctype_available("Weekly Timeslot"):
		frappe.throw(_("Weekly Timeslot is not installed on this site."))
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	payload = _document_payload(doc)
	payload["enrollments"] = _get_enrollment_rows(filters={"weekly_timeslot": weekly_timeslot, "status": "Active"}, limit=200)
	payload["sessions"] = _get_course_session_rows(weekly_timeslot=weekly_timeslot, limit=80)
	return payload


def get_school_admin_course_sessions_data(
	weekly_timeslot=None,
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	limit=160,
):
	_require_school_admin()
	if not _doctype_available("Course Sessions"):
		return {"items": []}
	return {
		"items": _get_course_session_rows(
			weekly_timeslot=weekly_timeslot,
			term=term,
			course=course,
			campus=campus,
			from_date=from_date,
			to_date=to_date,
			limit=_limit(limit, default=160, max_value=300),
		)
	}


def get_school_admin_course_session_data(course_session=None):
	_require_school_admin()
	if not _doctype_available("Course Sessions"):
		frappe.throw(_("Course Sessions is not installed on this site."))
	if not course_session:
		frappe.throw(_("Course session is required."))
	doc = frappe.get_doc("Course Sessions", course_session)
	payload = _document_payload(doc)
	payload["attendance"] = [_child_payload(row) for row in doc.get("attendance_list", [])]
	if payload.get("weekly_timeslot"):
		payload["weekly_timeslot_detail"] = _get_timeslot_summary(payload.get("weekly_timeslot"))
	return payload


def _require_school_admin():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can access School Admin APIs."), frappe.PermissionError)


def _get_payload(payload=None):
	if payload is None:
		payload = frappe.form_dict.get("payload")
	if isinstance(payload, str):
		return json.loads(payload) if payload.strip() else {}
	return payload or {}


def _limit(value, default=80, max_value=200):
	value = cint(value or default)
	if value <= 0:
		value = default
	return min(value, max_value)


def _count(doctype, filters):
	if not _doctype_available(doctype):
		return 0
	try:
		return frappe.db.count(doctype, filters)
	except Exception:
		return 0


def _count_sales_invoices(filters):
	if not _doctype_available("Sales Invoice"):
		return 0
	return _count("Sales Invoice", filters)


def _doctype_available(doctype):
	try:
		return bool(frappe.db.exists("DocType", doctype)) and bool(frappe.db.table_exists(doctype))
	except Exception:
		return False


def _safe_fields(doctype, candidates):
	fields = []
	for fieldname in candidates:
		if fieldname == "name" or _has_field(doctype, fieldname):
			fields.append(fieldname)
	return fields or ["name"]


def _has_field(doctype, fieldname):
	try:
		if fieldname in {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}:
			return True
		if not _doctype_available(doctype):
			return False
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def _field_value(doc_or_row, fieldname):
	if hasattr(doc_or_row, "get"):
		return doc_or_row.get(fieldname)
	return getattr(doc_or_row, fieldname, None)


def _docdict(row):
	return dict(row) if isinstance(row, dict) else row.as_dict()


def _document_payload(doc):
	data = {}
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}:
			continue
		if field.fieldtype == "Table":
			data[field.fieldname] = [_child_payload(row) for row in doc.get(field.fieldname, [])]
		else:
			value = doc.get(field.fieldname)
			data[field.fieldname] = str(value) if hasattr(value, "isoformat") else value
	data["name"] = doc.name
	data["doctype"] = doc.doctype
	return data


def _child_payload(row):
	data = row.as_dict()
	for key, value in list(data.items()):
		if hasattr(value, "isoformat"):
			data[key] = str(value)
	return data


def _search_parents(query, limit):
	if not _doctype_available("Parent"):
		return []
	fields = _safe_fields("Parent", ["name", "parent_name", "mobile_number", "email", "customer"])
	return _search_doctype("Parent", query, fields, ["name", "parent_name", "mobile_number", "email"], limit)


def _search_students(query, limit):
	if not _doctype_available("Student"):
		return []
	fields = _safe_fields("Student", ["name", "student_name", "guardian", "date_of_birth", "status"])
	return _search_doctype("Student", query, fields, ["name", "student_name", "guardian"], limit)


def _search_customers(query, limit):
	if not _doctype_available("Customer"):
		return []
	fields = _safe_fields("Customer", ["name", "customer_name", "email_id", "mobile_no", "customer_type"])
	return _search_doctype("Customer", query, fields, ["name", "customer_name", "email_id", "mobile_no"], limit)


def _search_inquiries(query, limit):
	if not _doctype_available("Inquiry"):
		return []
	fields = [
		"name",
		"inquiry_type",
		"status",
		"campus",
		"parent",
		"student",
		"contact_name",
		"contact_phone",
		"contact_email",
		"current_appointment_date",
	]
	return _search_doctype("Inquiry", query, fields, ["name", "parent", "student", "contact_name", "contact_phone", "contact_email"], limit)


def _search_enrollments(query, limit):
	if not _doctype_available("Enrollment"):
		return []
	fields = _safe_fields(
		"Enrollment",
		["name", "student", "parent", "term", "course", "weekly_timeslot", "enrollment_type", "status", "invoice"],
	)
	return _search_doctype("Enrollment", query, fields, ["name", "student", "parent", "course", "weekly_timeslot", "invoice"], limit)


def _search_invoices(query, limit):
	if not _doctype_available("Sales Invoice"):
		return []
	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "posting_date", "due_date", "status", "docstatus", "grand_total", "outstanding_amount"],
	)
	return _search_doctype("Sales Invoice", query, fields, ["name", "customer", "status"], limit)


def _search_doctype(doctype, query, fields, search_fields, limit):
	search_fields = [fieldname for fieldname in search_fields if fieldname == "name" or _has_field(doctype, fieldname)]
	if not search_fields:
		return []
	or_filters = [[doctype, fieldname, "like", f"%{query}%"] for fieldname in search_fields]
	try:
		rows = frappe.get_all(
			doctype,
			or_filters=or_filters,
			fields=fields,
			limit=limit,
			order_by="modified desc",
		)
	except Exception:
		return []
	return [_normalize_row_payload(doctype, row) for row in rows]


def _normalize_row_payload(doctype, row):
	data = _docdict(row)
	for key, value in list(data.items()):
		if hasattr(value, "isoformat"):
			data[key] = str(value)
	data["doctype"] = doctype
	return data


def _resolve_family_context(parent=None, student=None, customer=None, email=None):
	context = {"parent": parent, "student": student, "customer": customer}
	if student and not parent and _has_field("Student", "guardian"):
		context["parent"] = frappe.db.get_value("Student", student, "guardian")
	if parent and not customer and _has_field("Parent", "customer"):
		context["customer"] = frappe.db.get_value("Parent", parent, "customer")
	if customer and not parent and _has_field("Parent", "customer"):
		context["parent"] = frappe.db.get_value("Parent", {"customer": customer}, "name")
	if email and not context.get("parent"):
		context["parent"] = _find_parent_by_email(email)
	if email and not context.get("customer"):
		context["customer"] = _find_customer_by_email(email)
	if context.get("parent") and not context.get("customer") and _has_field("Parent", "customer"):
		context["customer"] = frappe.db.get_value("Parent", context["parent"], "customer")
	return context


def _find_parent_by_email(email):
	for fieldname in ["email", "email_id", "contact_email"]:
		if _has_field("Parent", fieldname):
			parent = frappe.db.get_value("Parent", {fieldname: email}, "name")
			if parent:
				return parent
	return None


def _find_customer_by_email(email):
	for fieldname in ["email_id", "email", "contact_email"]:
		if _has_field("Customer", fieldname):
			customer = frappe.db.get_value("Customer", {fieldname: email}, "name")
			if customer:
				return customer
	return None


def _get_parent_payload(parent):
	fields = _safe_fields("Parent", ["name", "parent_name", "mobile_number", "email", "customer", "status"])
	rows = frappe.get_all("Parent", filters={"name": parent}, fields=fields, limit=1)
	return _normalize_row_payload("Parent", rows[0]) if rows else {"doctype": "Parent", "name": parent}


def _get_customer_payload(customer):
	fields = _safe_fields("Customer", ["name", "customer_name", "email_id", "mobile_no", "customer_type", "customer_group"])
	rows = frappe.get_all("Customer", filters={"name": customer}, fields=fields, limit=1)
	return _normalize_row_payload("Customer", rows[0]) if rows else {"doctype": "Customer", "name": customer}


def _get_family_students(parent=None, student=None):
	if student:
		filters = {"name": student}
	elif parent and _has_field("Student", "guardian"):
		filters = {"guardian": parent}
	else:
		return []
	fields = _safe_fields("Student", ["name", "student_name", "guardian", "date_of_birth", "status", "gender"])
	rows = frappe.get_all("Student", filters=filters, fields=fields, order_by="student_name asc")
	return [_normalize_row_payload("Student", row) for row in rows]


def _get_family_inquiry_rows(parent=None, students=None, email=None, limit=80):
	if not _doctype_available("Inquiry"):
		return []
	or_filters = []
	if parent:
		or_filters.append(["Inquiry", "parent", "=", parent])
	for student in students or []:
		or_filters.append(["Inquiry", "student", "=", student])
	if email:
		or_filters.append(["Inquiry", "contact_email", "=", email])
	if not or_filters:
		return []
	fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"inquiry_type",
			"status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"preferred_course",
			"current_appointment_date",
			"current_appointment_time",
			"converted_enrollment",
			"converted_invoice",
		],
	)
	rows = frappe.get_all(
		"Inquiry",
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return [_build_inquiry_list_item(row) for row in rows]


def _build_inquiry_list_item(row):
	payload = build_inquiry_summary(row)
	payload["latest_note"] = _get_latest_inquiry_note(row.name)
	return payload


def _get_latest_inquiry_note(inquiry):
	rows = frappe.get_all(
		"Inquiry Note",
		filters={"inquiry": inquiry},
		fields=["note", "creation"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0].note if rows else None


def _inquiry_order_by(queue):
	if queue == "post_visit":
		return "current_appointment_date desc, modified desc"
	return "current_appointment_date asc, current_appointment_time asc, modified desc"


def _get_invoice_rows(status=None, customer=None, parent=None, students=None, source=None, limit=80):
	if not _doctype_available("Sales Invoice"):
		return []
	filters = {}
	if status:
		_apply_invoice_status_filter(filters, status)
	if customer:
		filters["customer"] = customer
	if parent and _has_field("Sales Invoice", "parent"):
		filters["parent"] = parent
	if students and _has_field("Sales Invoice", "student"):
		filters["student"] = ["in", students]
	if source:
		_apply_invoice_source_filter(filters, source)
	fields = _safe_fields(
		"Sales Invoice",
		[
			"name",
			"customer",
			"posting_date",
			"due_date",
			"status",
			"docstatus",
			"grand_total",
			"outstanding_amount",
			"parent",
			"student",
			"enrollment",
			"course",
			"term",
			"source_inquiry",
		],
	)
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return [_normalize_row_payload("Sales Invoice", row) for row in rows]


def _apply_invoice_status_filter(filters, status):
	status = status.strip()
	if status == "Draft":
		filters["docstatus"] = 0
	elif status == "Submitted":
		filters["docstatus"] = 1
	elif status == "Cancelled":
		filters["docstatus"] = 2
	elif _has_field("Sales Invoice", "status"):
		filters["status"] = status


def _apply_invoice_source_filter(filters, source):
	if source == "Inquiry" and _has_field("Sales Invoice", "source_inquiry"):
		filters["source_inquiry"] = ["is", "set"]
	elif source == "Enrollment" and _has_field("Sales Invoice", "enrollment"):
		filters["enrollment"] = ["is", "set"]
	elif source == "Manual" and _has_field("Sales Invoice", "source_type"):
		filters["source_type"] = "Manual"


def _build_invoice_payload(doc):
	doc = frappe.get_doc("Sales Invoice", doc) if isinstance(doc, str) else doc
	payload = _document_payload(doc)
	payload["docstatus"] = cint(doc.docstatus)
	payload["status_label"] = _invoice_status_label(doc)
	payload["items"] = [_child_payload(row) for row in doc.get("items", [])]
	payload["comments"] = _get_comments("Sales Invoice", doc.name)
	return payload


def _invoice_status_label(doc):
	if cint(doc.docstatus) == 0:
		return doc.get("status") or "Draft"
	if cint(doc.docstatus) == 1:
		return doc.get("status") or "Submitted"
	if cint(doc.docstatus) == 2:
		return "Cancelled"
	return doc.get("status")


def _apply_invoice_items(invoice, items):
	if not items:
		frappe.throw(_("At least one invoice item is required."))
	invoice.set("items", [])
	for row in items:
		item_code = row.get("item_code") or row.get("item")
		if not item_code:
			frappe.throw(_("Invoice item code is required."))
		invoice.append(
			"items",
			{
				"item_code": item_code,
				"item_name": row.get("item_name") or item_code,
				"description": row.get("description") or row.get("item_name") or item_code,
				"qty": flt(row.get("qty") or 1),
				"rate": flt(row.get("rate") or 0),
			},
		)


def _mark_draft_invoice_cancelled(doc, reason):
	_add_comment("Sales Invoice", doc.name, f"Draft invoice marked cancelled by School Admin. Reason: {reason}")
	if _has_field("Sales Invoice", "status"):
		frappe.db.set_value("Sales Invoice", doc.name, "status", "Cancelled", update_modified=True)
	if _has_field("Sales Invoice", "cancel_reason"):
		frappe.db.set_value("Sales Invoice", doc.name, "cancel_reason", reason, update_modified=False)
	elif _has_field("Sales Invoice", "cancellation_reason"):
		frappe.db.set_value("Sales Invoice", doc.name, "cancellation_reason", reason, update_modified=False)


def _get_enrollment_rows(parent=None, students=None, filters=None, limit=80):
	if not _doctype_available("Enrollment"):
		return []
	filters = dict(filters or {})
	if parent:
		filters["parent"] = parent
	if students:
		filters["student"] = ["in", students]
	fields = _safe_fields(
		"Enrollment",
		[
			"name",
			"student",
			"parent",
			"term",
			"course",
			"weekly_timeslot",
			"start_course_session",
			"enrollment_type",
			"status",
			"enrollment_date",
			"invoice",
			"invoice_status",
			"invoice_amount",
			"remaining_sessions",
			"source_inquiry",
		],
	)
	rows = frappe.get_all(
		"Enrollment",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return [_normalize_row_payload("Enrollment", row) for row in rows]


def _build_enrollment_payload(doc):
	payload = _document_payload(doc)
	if payload.get("weekly_timeslot"):
		payload["weekly_timeslot_detail"] = _get_timeslot_summary(payload.get("weekly_timeslot"))
	if payload.get("invoice"):
		payload["invoice_summary"] = _get_invoice_summary(payload.get("invoice"))
	return payload


def _get_invoice_summary(invoice):
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		return None
	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "posting_date", "due_date", "status", "docstatus", "grand_total", "outstanding_amount"],
	)
	rows = frappe.get_all("Sales Invoice", filters={"name": invoice}, fields=fields, limit=1)
	return _normalize_row_payload("Sales Invoice", rows[0]) if rows else None


def _get_course_session_rows(
	weekly_timeslot=None,
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	limit=160,
):
	if not _doctype_available("Course Sessions"):
		return []
	filters = {}
	if weekly_timeslot:
		filters["weekly_timeslot"] = weekly_timeslot
	if from_date and to_date:
		filters["session_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["session_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["session_date"] = ["<=", getdate(to_date)]
	timeslot_ids = _filter_timeslots_for_session_query(term=term, course=course, campus=campus)
	if timeslot_ids is not None:
		if weekly_timeslot and weekly_timeslot not in timeslot_ids:
			return []
		if not weekly_timeslot:
			filters["weekly_timeslot"] = ["in", timeslot_ids]
	fields = _safe_fields("Course Sessions", ["name", "weekly_timeslot", "session_date", "status", "modified"])
	rows = frappe.get_all(
		"Course Sessions",
		filters=filters,
		fields=fields,
		order_by="session_date asc, modified asc",
		limit=limit,
	)
	timeslot_map = _get_timeslot_map([row.weekly_timeslot for row in rows if row.get("weekly_timeslot")])
	items = []
	for row in rows:
		item = _normalize_row_payload("Course Sessions", row)
		item["weekly_timeslot_detail"] = timeslot_map.get(row.weekly_timeslot)
		items.append(item)
	return items


def _filter_timeslots_for_session_query(term=None, course=None, campus=None):
	if not _doctype_available("Weekly Timeslot"):
		return []
	if not any([term, course, campus]):
		return None
	filters = {}
	for fieldname, value in {"term": term, "course": course, "campus": campus}.items():
		if value and _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = value
	if not filters:
		return None
	return [row.name for row in frappe.get_all("Weekly Timeslot", filters=filters, fields=["name"])]


def _get_timeslot_map(timeslot_ids):
	if not _doctype_available("Weekly Timeslot"):
		return {}
	timeslot_ids = sorted({timeslot_id for timeslot_id in timeslot_ids if timeslot_id})
	if not timeslot_ids:
		return {}
	fields = _safe_fields(
		"Weekly Timeslot",
		["name", "term", "course", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time", "status"],
	)
	rows = frappe.get_all("Weekly Timeslot", filters={"name": ["in", timeslot_ids]}, fields=fields)
	return {row.name: _normalize_row_payload("Weekly Timeslot", row) for row in rows}


def _get_timeslot_summary(weekly_timeslot):
	return _get_timeslot_map([weekly_timeslot]).get(weekly_timeslot)


def _add_comment(reference_doctype, reference_name, content):
	try:
		comment = frappe.new_doc("Comment")
		comment.comment_type = "Info"
		comment.reference_doctype = reference_doctype
		comment.reference_name = reference_name
		comment.content = content
		comment.comment_by = frappe.session.user
		comment.insert(ignore_permissions=True)
	except Exception:
		pass


def _get_comments(reference_doctype, reference_name, limit=20):
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=["name", "comment_type", "content", "comment_by", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	return [_normalize_row_payload("Comment", row) for row in rows]


def _set_if_field(doc, fieldname, value):
	if fieldname and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)
