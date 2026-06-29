from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

from qas_custom.modules.billing.commands import (
	get_course_money,
	get_course_number,
	get_invoice_customer,
	get_invoice_item,
)
from qas_custom.modules.billing.invoice_settings import (
	apply_default_invoice_dates,
	apply_invoice_payment_snapshot,
)
from qas_custom.modules.billing.presentation import build_course_invoice_description
from qas_custom.services.display_labels import get_student_display_code, get_student_parent_name
from qas_custom.services.school_admin import (
	_apply_enrollment_payload,
	_bulk_action_error_message,
	_count,
	_create_enrollment_attendance_entries,
	_doctype_available,
	_document_payload,
	_ensure_course_session,
	_get_payload,
	_has_field,
	_limit,
	_normalize_row_payload,
	_require_school_admin,
	_safe_fields,
	_set_if_field,
	_sync_invoice_student_summary,
	_weekday_number,
	get_school_admin_weekly_timeslots_data,
)


TIMESLOT_FIELDS = [
	"term",
	"course",
	"campus",
	"classroom",
	"teacher",
	"day_of_week",
	"start_time",
	"end_time",
]


def get_terms_data(status=None, limit=80):
	_require_school_admin()
	if not _doctype_available("Term"):
		return {"items": []}
	filters = {}
	if status:
		filters["status"] = status
	fields = _safe_fields("Term", ["name", "term_name", "start_date", "end_date", "status", "modified"])
	rows = frappe.get_all(
		"Term",
		filters=filters,
		fields=fields,
		order_by="start_date desc, modified desc",
		limit=_limit(limit, default=80, max_value=200),
	)
	return {"items": [_term_row_payload(row) for row in rows]}


def get_term_data(term=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	doc = frappe.get_doc("Term", term)
	payload = _document_payload(doc)
	payload["weekly_timeslot_count"] = _count("Weekly Timeslot", {"term": term})
	timeslot_ids = frappe.get_all("Weekly Timeslot", filters={"term": term}, pluck="name", limit_page_length=0)
	payload["course_session_count"] = (
		_count("Course Sessions", {"weekly_timeslot": ["in", timeslot_ids]}) if timeslot_ids else 0
	)
	payload["active_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Active"})
	payload["rollover_plans"] = _get_rollover_plan_rows(target_term=term, limit=20)
	return payload


def create_term_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	doc = frappe.new_doc("Term")
	for fieldname in ["term_name", "start_date", "end_date", "status", "notes"]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if not doc.get("term_name"):
		frappe.throw(_("Term name is required."))
	if not doc.get("start_date") or not doc.get("end_date"):
		frappe.throw(_("Term start and end dates are required."))
	if getdate(doc.end_date) < getdate(doc.start_date):
		frappe.throw(_("Term end date cannot be before start date."))
	if not doc.get("status"):
		_set_if_field(doc, "status", "Upcoming")
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return get_term_data(doc.name)


def copy_term_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	source_term = payload.get("source_term")
	target_term = payload.get("target_term")
	if not source_term or not target_term:
		frappe.throw(_("Source term and target term are required."))
	if source_term == target_term:
		frappe.throw(_("Source term and target term must be different."))
	if not frappe.db.exists("Term", source_term) or not frappe.db.exists("Term", target_term):
		frappe.throw(_("Source or target term does not exist."))

	timeslot_map = _copy_term_weekly_timeslots(source_term, target_term)
	plan = frappe.new_doc("Term Rollover Plan")
	plan.source_term = source_term
	plan.target_term = target_term
	plan.status = "Draft"
	plan.copied_timeslot_count = len(set(timeslot_map.values()))

	planned_count = 0
	for enrollment in _source_term_rollover_enrollments(source_term):
		target_timeslot = timeslot_map.get(enrollment.get("weekly_timeslot"))
		row = plan.append("planned_rows", {})
		row.source_enrollment = enrollment.get("name")
		row.student = enrollment.get("student")
		row.parent = enrollment.get("parent")
		row.source_weekly_timeslot = enrollment.get("weekly_timeslot")
		row.target_weekly_timeslot = target_timeslot
		row.course = enrollment.get("course")
		row.action = "Continue"
		row.status = "Planned" if target_timeslot else "Error"
		if not target_timeslot:
			row.error_message = _("Target weekly timeslot was not copied.")
		planned_count += 1

	plan.planned_enrollment_count = planned_count
	plan.insert(ignore_permissions=True)
	frappe.db.commit()
	return get_rollover_plan_data(plan.name)


def get_rollover_plan_data(plan=None):
	_require_school_admin()
	if not plan:
		frappe.throw(_("Rollover plan is required."))
	doc = frappe.get_doc("Term Rollover Plan", plan)
	payload = _document_payload(doc)
	payload["source_term_detail"] = _term_summary(doc.source_term)
	payload["target_term_detail"] = _term_summary(doc.target_term)
	payload["target_weekly_timeslots"] = get_school_admin_weekly_timeslots_data(term=doc.target_term, limit=300).get("items", [])
	return payload


def update_rollover_plan_row_data(plan=None, row=None, payload=None):
	_require_school_admin()
	if not plan:
		frappe.throw(_("Rollover plan is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Term Rollover Plan", plan)
	if doc.status != "Draft":
		frappe.throw(_("Only draft rollover plans can be edited."))
	target = None
	if row:
		target = next((item for item in doc.get("planned_rows", []) if item.name == row), None)
		if not target:
			frappe.throw(_("Rollover plan row was not found."))
	else:
		target = doc.append("planned_rows", {})

	for fieldname in ["student", "parent", "target_weekly_timeslot", "course", "action", "status", "notes"]:
		if fieldname in payload:
			target.set(fieldname, payload.get(fieldname))
	if not target.get("student"):
		frappe.throw(_("Student is required."))
	if target.get("action") == "Drop":
		target.status = "Excluded"
	elif target.get("status") == "Excluded" and target.get("action") != "Drop":
		target.status = "Planned"
	if not target.get("action"):
		target.action = "New" if not target.get("source_enrollment") else "Continue"
	if not target.get("status"):
		target.status = "Planned"

	doc.planned_enrollment_count = len(doc.get("planned_rows", []))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return get_rollover_plan_data(doc.name)


def populate_term_data(plan=None):
	_require_school_admin()
	if not plan:
		frappe.throw(_("Rollover plan is required."))
	doc = frappe.get_doc("Term Rollover Plan", plan)
	if doc.status == "Cancelled":
		frappe.throw(_("Cancelled rollover plans cannot be populated."))
	target_term = frappe.get_doc("Term", doc.target_term)
	created_sessions = _generate_sessions_for_term(doc.target_term)
	created_enrollments = 0
	created_invoices = 0
	skipped = 0
	errors = 0

	for row in doc.get("planned_rows", []):
		if row.get("action") == "Drop" or row.get("status") == "Excluded":
			row.status = "Excluded"
			skipped += 1
			continue
		if row.get("created_enrollment"):
			skipped += 1
			continue
		savepoint = f"rollover_row_{row.idx or 0}"
		frappe.db.savepoint(savepoint)
		try:
			result = _populate_rollover_plan_row(row, doc.target_term, target_term)
			row.created_enrollment = result.get("enrollment")
			row.created_invoice = result.get("invoice")
			row.status = "Populated"
			row.error_message = ""
			created_enrollments += 1
			if result.get("invoice"):
				created_invoices += 1
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			row.status = "Error"
			row.error_message = _bulk_action_error_message(exc)
			errors += 1

	doc.created_session_count = cint(doc.get("created_session_count")) + len(created_sessions)
	doc.created_enrollment_count = cint(doc.get("created_enrollment_count")) + created_enrollments
	doc.created_invoice_count = cint(doc.get("created_invoice_count")) + created_invoices
	if errors == 0:
		doc.status = "Populated"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"plan": get_rollover_plan_data(doc.name),
		"summary": {
			"created_sessions": len(created_sessions),
			"created_enrollments": created_enrollments,
			"created_invoices": created_invoices,
			"skipped": skipped,
			"errors": errors,
		},
	}


def _term_row_payload(row):
	payload = _normalize_row_payload("Term", row)
	term = payload.get("name")
	if term:
		payload["weekly_timeslot_count"] = _count("Weekly Timeslot", {"term": term})
		payload["active_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Active"})
	return payload


def _term_summary(term):
	if not term or not frappe.db.exists("Term", term):
		return None
	fields = _safe_fields("Term", ["name", "term_name", "start_date", "end_date", "status"])
	rows = frappe.get_all("Term", filters={"name": term}, fields=fields, limit=1)
	return _term_row_payload(rows[0]) if rows else None


def _get_rollover_plan_rows(target_term=None, source_term=None, limit=20):
	if not _doctype_available("Term Rollover Plan"):
		return []
	filters = {}
	if target_term:
		filters["target_term"] = target_term
	if source_term:
		filters["source_term"] = source_term
	fields = _safe_fields(
		"Term Rollover Plan",
		[
			"name",
			"source_term",
			"target_term",
			"status",
			"copied_timeslot_count",
			"planned_enrollment_count",
			"created_session_count",
			"created_enrollment_count",
			"created_invoice_count",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Term Rollover Plan",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return [_normalize_row_payload("Term Rollover Plan", row) for row in rows]


def _copy_term_weekly_timeslots(source_term, target_term):
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
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": source_term},
		fields=fields,
		order_by="course asc, campus asc, day_of_week asc, start_time asc",
		limit_page_length=0,
	)
	timeslot_map = {}
	for row in rows:
		existing = _matching_target_timeslot(row, target_term)
		if existing:
			timeslot_map[row.name] = existing
			continue
		doc = frappe.new_doc("Weekly Timeslot")
		for fieldname in fields:
			if fieldname in {"name", "term"}:
				continue
			_set_if_field(doc, fieldname, row.get(fieldname))
		_set_if_field(doc, "term", target_term)
		doc.insert(ignore_permissions=True)
		timeslot_map[row.name] = doc.name
	return timeslot_map


def _matching_target_timeslot(source_row, target_term):
	filters = {"term": target_term}
	for fieldname in ["course", "campus", "classroom", "teacher", "day_of_week", "start_time"]:
		if _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = source_row.get(fieldname)
	return frappe.db.exists("Weekly Timeslot", filters)


def _source_term_rollover_enrollments(source_term):
	fields = _safe_fields(
		"Enrollment",
		[
			"name",
			"student",
			"parent",
			"term",
			"course",
			"weekly_timeslot",
			"enrollment_type",
			"status",
		],
	)
	return frappe.get_all(
		"Enrollment",
		filters={"term": source_term, "status": "Active", "enrollment_type": "Full-Term"},
		fields=fields,
		order_by="weekly_timeslot asc, student asc",
		limit_page_length=0,
	)


def _generate_sessions_for_term(term):
	term_doc = frappe.get_doc("Term", term)
	if not term_doc.get("start_date") or not term_doc.get("end_date"):
		frappe.throw(_("Target term dates are required before generating sessions."))
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": term},
		fields=_safe_fields("Weekly Timeslot", ["name", "day_of_week"]),
		limit_page_length=0,
	)
	created = []
	for timeslot in timeslots:
		current = getdate(term_doc.start_date)
		end_date = getdate(term_doc.end_date)
		target_weekday = _weekday_number(timeslot.day_of_week)
		while current <= end_date:
			if current.weekday() == target_weekday:
				session = _ensure_course_session(timeslot.name, current)
				if session.get("created"):
					created.append(session.get("name"))
			current = current + timedelta(days=1)
	return created


def _populate_rollover_plan_row(row, target_term, target_term_doc):
	if not row.get("student"):
		frappe.throw(_("Student is required."))
	if not row.get("target_weekly_timeslot"):
		frappe.throw(_("Target weekly timeslot is required."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		row.target_weekly_timeslot,
		_safe_fields("Weekly Timeslot", ["name", "term", "course"]),
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Target weekly timeslot does not exist."))
	if timeslot.get("term") != target_term:
		frappe.throw(_("Target weekly timeslot does not belong to the target term."))
	if _duplicate_active_enrollment(row.student, target_term, row.target_weekly_timeslot):
		frappe.throw(_("An active enrollment already exists for this student and weekly timeslot."))

	start_session = _first_course_session_for_timeslot(row.target_weekly_timeslot, target_term_doc)
	parent = row.get("parent") or frappe.db.get_value("Student", row.student, "guardian")
	course = row.get("course") or timeslot.get("course")

	enrollment = frappe.new_doc("Enrollment")
	_apply_enrollment_payload(
		enrollment,
		{
			"student": row.student,
			"parent": parent,
			"term": target_term,
			"course": course,
			"weekly_timeslot": row.target_weekly_timeslot,
			"start_course_session": start_session,
			"enrollment_type": "Full-Term",
			"status": "Active",
			"enrollment_date": target_term_doc.get("start_date") or today(),
			"source_inquiry": None,
		},
	)
	enrollment.insert(ignore_permissions=True)
	_create_enrollment_attendance_entries(enrollment)
	invoice = _create_rollover_enrollment_invoice(enrollment, start_session)
	if invoice:
		_set_if_field(enrollment, "invoice", invoice.name)
		_set_if_field(enrollment, "invoice_status", "Draft")
		_set_if_field(enrollment, "invoice_amount", invoice.get("grand_total"))
		enrollment.save(ignore_permissions=True)
	return {"enrollment": enrollment.name, "invoice": invoice.name if invoice else None}


def _duplicate_active_enrollment(student, term, weekly_timeslot):
	return frappe.db.exists(
		"Enrollment",
		{
			"student": student,
			"term": term,
			"weekly_timeslot": weekly_timeslot,
			"status": "Active",
		},
	)


def _first_course_session_for_timeslot(weekly_timeslot, term_doc):
	rows = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": weekly_timeslot,
			"session_date": ["between", [getdate(term_doc.start_date), getdate(term_doc.end_date)]],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "session_date"],
		order_by="session_date asc",
		limit=1,
	)
	if not rows:
		frappe.throw(_("No course sessions were generated for the target weekly timeslot."))
	return rows[0].name


def _create_rollover_enrollment_invoice(enrollment, start_session):
	parent = enrollment.get("parent")
	course = enrollment.get("course")
	if not parent or not course:
		frappe.throw(_("Parent and course are required before generating an invoice."))
	customer = get_invoice_customer(parent)
	item_code = get_invoice_item(course)
	session_count = _course_session_count_for_enrollment(enrollment, start_session)
	if session_count <= 0:
		frappe.throw(_("No billable sessions found for enrollment."))
	full_term_fee = get_course_money(course, ("full_term_fee", "full_term_price", "term_fee"))
	if full_term_fee <= 0:
		frappe.throw(_("Course full term fee is required before generating an invoice."))
	total_sessions = get_course_number(course, ("total_session_per_term", "total_sessions_per_term", "sessions_per_term")) or session_count
	unit_rate = flt(full_term_fee) / flt(total_sessions)

	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	apply_default_invoice_dates(invoice)
	_set_if_field(invoice, "parent", parent)
	_set_if_field(invoice, "qas_invoice_type", "Course")
	_set_if_field(invoice, "source_doctype", "Enrollment")
	_set_if_field(invoice, "source_document", enrollment.name)
	_set_if_field(invoice, "enrollment", enrollment.name)
	_set_if_field(invoice, "billing_note", _("Draft course invoice generated from term rollover."))

	student_name = get_student_parent_name(enrollment.student) or enrollment.student
	student_code = get_student_display_code(enrollment.student) or enrollment.student
	description = build_course_invoice_description(student_name, course, enrollment.term, session_count)
	item = invoice.append(
		"items",
		{
			"item_code": item_code,
			"item_name": course,
			"description": description,
			"qty": session_count,
			"rate": unit_rate,
		},
	)
	_set_if_field(item, "qas_line_type", "Course Fee")
	_set_if_field(item, "student", enrollment.student)
	_set_if_field(item, "student_display_name", student_name)
	_set_if_field(item, "student_code", student_code)
	_set_if_field(item, "enrollment", enrollment.name)
	_set_if_field(item, "course", course)
	_set_if_field(item, "term", enrollment.term)
	_set_if_field(item, "course_session", start_session)
	_set_if_field(item, "session_count", session_count)
	_sync_invoice_student_summary(invoice)
	apply_invoice_payment_snapshot(invoice)
	invoice.insert(ignore_permissions=True)
	return invoice


def _course_session_count_for_enrollment(enrollment, start_session):
	start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
	if not start_date:
		return 0
	return frappe.db.count(
		"Course Sessions",
		{
			"weekly_timeslot": enrollment.weekly_timeslot,
			"session_date": [">=", getdate(start_date)],
			"status": ["!=", "Cancelled"],
		},
	)


def copy_term_setup_data(source_term=None, target_term=None, dry_run=1):
	dry_run = cint(dry_run) == 1
	source_term = _clean_name(source_term)
	target_term = _clean_name(target_term)

	_require_system_manager()
	_validate_terms(source_term, target_term)

	target_start_date = frappe.db.get_value("Term", target_term, "start_date")
	source_timeslots = _get_source_timeslots(source_term)

	timeslot_map = {}
	timeslot_results = []
	for source_timeslot in source_timeslots:
		target_timeslot_name = _get_existing_target_timeslot(source_timeslot, target_term)
		action = "reused"

		if not target_timeslot_name:
			action = "would_create" if dry_run else "created"
			target_timeslot_name = _build_target_timeslot_name(source_timeslot, target_term)
			if not dry_run:
				target_timeslot_name = _create_target_timeslot(source_timeslot, target_term)

		timeslot_map[source_timeslot["name"]] = target_timeslot_name
		timeslot_results.append(
			{
				"source": source_timeslot["name"],
				"target": target_timeslot_name,
				"action": action,
				"course": source_timeslot.get("course"),
				"day_of_week": source_timeslot.get("day_of_week"),
				"start_time": str(source_timeslot.get("start_time") or ""),
				"teacher": source_timeslot.get("teacher"),
			}
		)

	enrollment_results = _copy_full_term_enrollments(
		source_term=source_term,
		target_term=target_term,
		target_start_date=target_start_date,
		timeslot_map=timeslot_map,
		dry_run=dry_run,
	)

	if not dry_run:
		frappe.db.commit()

	return {
		"dry_run": dry_run,
		"source_term": source_term,
		"target_term": target_term,
		"weekly_timeslots": _summarize(items=timeslot_results),
		"enrollments": _summarize(items=enrollment_results),
		"timeslot_items": timeslot_results,
		"enrollment_items": enrollment_results,
	}


def _require_system_manager():
	if frappe.session.user == "Administrator":
		return

	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only System Manager users can copy term setup."), frappe.PermissionError)


def _validate_terms(source_term: str | None, target_term: str | None):
	if not source_term:
		frappe.throw(_("Source term is required."))
	if not target_term:
		frappe.throw(_("Target term is required."))
	if source_term == target_term:
		frappe.throw(_("Source term and target term must be different."))
	if not frappe.db.exists("Term", source_term):
		frappe.throw(_("Source term {0} was not found.").format(source_term))
	if not frappe.db.exists("Term", target_term):
		frappe.throw(_("Target term {0} was not found.").format(target_term))


def _get_source_timeslots(source_term: str):
	return frappe.get_all(
		"Weekly Timeslot",
		filters={"term": source_term},
		fields=TIMESLOT_FIELDS + ["name"],
		order_by="course asc, campus asc, day_of_week asc, start_time asc, teacher asc",
	)


def _get_existing_target_timeslot(source_timeslot: dict, target_term: str):
	return frappe.db.exists(
		"Weekly Timeslot",
		{
			"term": target_term,
			"course": source_timeslot.get("course"),
			"campus": source_timeslot.get("campus"),
			"classroom": source_timeslot.get("classroom"),
			"teacher": source_timeslot.get("teacher"),
			"day_of_week": source_timeslot.get("day_of_week"),
			"start_time": source_timeslot.get("start_time"),
		},
	)


def _build_target_timeslot_name(source_timeslot: dict, target_term: str):
	parts = [
		target_term,
		source_timeslot.get("course"),
		source_timeslot.get("campus"),
		source_timeslot.get("day_of_week"),
		source_timeslot.get("start_time"),
		source_timeslot.get("teacher"),
	]
	return "-".join(str(part) for part in parts if part)


def _create_target_timeslot(source_timeslot: dict, target_term: str):
	doc = frappe.new_doc("Weekly Timeslot")
	for fieldname in TIMESLOT_FIELDS:
		doc.set(fieldname, source_timeslot.get(fieldname))
	doc.term = target_term
	doc.insert()
	return doc.name


def _copy_full_term_enrollments(source_term, target_term, target_start_date, timeslot_map, dry_run):
	source_enrollments = frappe.get_all(
		"Enrollment",
		filters={
			"term": source_term,
			"status": "Active",
			"enrollment_type": "Full-Term",
		},
		fields=[
			"name",
			"student",
			"course",
			"weekly_timeslot",
			"enrollment_type",
			"status",
		],
		order_by="student asc, weekly_timeslot asc",
	)

	results = []
	for source_enrollment in source_enrollments:
		target_timeslot = timeslot_map.get(source_enrollment.get("weekly_timeslot"))
		if not target_timeslot:
			results.append(
				{
					"source": source_enrollment["name"],
					"target": None,
					"action": "skipped_missing_timeslot",
					"student": source_enrollment.get("student"),
					"course": source_enrollment.get("course"),
				}
			)
			continue

		existing_enrollment = _get_existing_target_enrollment(
			source_enrollment=source_enrollment,
			target_term=target_term,
			target_timeslot=target_timeslot,
		)
		target_enrollment_name = existing_enrollment.get("name") if existing_enrollment else None
		action = "reused"

		if existing_enrollment and (
			existing_enrollment.get("status") != "Active"
			or existing_enrollment.get("enrollment_type") != "Full-Term"
		):
			results.append(
				{
					"source": source_enrollment["name"],
					"target": target_enrollment_name,
					"action": "skipped_existing_conflict",
					"student": source_enrollment.get("student"),
					"course": source_enrollment.get("course"),
					"weekly_timeslot": target_timeslot,
					"existing_status": existing_enrollment.get("status"),
					"existing_enrollment_type": existing_enrollment.get("enrollment_type"),
				}
			)
			continue

		if not target_enrollment_name:
			action = "would_create" if dry_run else "created"
			target_enrollment_name = _build_target_enrollment_name(source_enrollment, target_timeslot)
			if not dry_run:
				target_enrollment_name = _create_target_enrollment(
					source_enrollment=source_enrollment,
					target_term=target_term,
					target_timeslot=target_timeslot,
					target_start_date=target_start_date,
				)

		results.append(
			{
				"source": source_enrollment["name"],
				"target": target_enrollment_name,
				"action": action,
				"student": source_enrollment.get("student"),
				"course": source_enrollment.get("course"),
				"weekly_timeslot": target_timeslot,
				"enrollment_date": str(getdate(target_start_date)) if target_start_date else None,
			}
		)

	return results


def _get_existing_target_enrollment(source_enrollment: dict, target_term: str, target_timeslot: str):
	existing_name = frappe.db.exists(
		"Enrollment",
		{
			"student": source_enrollment.get("student"),
			"term": target_term,
			"course": source_enrollment.get("course"),
			"weekly_timeslot": target_timeslot,
		},
	)
	if not existing_name:
		return None

	return frappe.db.get_value(
		"Enrollment",
		existing_name,
		["name", "status", "enrollment_type"],
		as_dict=True,
	)


def _build_target_enrollment_name(source_enrollment: dict, target_timeslot: str):
	return "-".join(
		str(part)
		for part in [
			source_enrollment.get("student"),
			target_timeslot,
		]
		if part
	)


def _create_target_enrollment(source_enrollment, target_term, target_timeslot, target_start_date):
	doc = frappe.new_doc("Enrollment")
	doc.student = source_enrollment.get("student")
	doc.term = target_term
	doc.course = source_enrollment.get("course")
	doc.weekly_timeslot = target_timeslot
	doc.enrollment_type = "Full-Term"
	doc.status = "Active"
	doc.enrollment_date = getdate(target_start_date) if target_start_date else None
	doc.insert()
	return doc.name


def _summarize(items: list[dict]):
	summary = {
		"total": len(items),
		"created": 0,
		"would_create": 0,
		"reused": 0,
		"skipped_missing_timeslot": 0,
		"skipped_existing_conflict": 0,
	}
	for item in items:
		action = item.get("action")
		if action in summary:
			summary[action] += 1
	return summary


def _clean_name(value):
	if value is None:
		return None
	value = str(value).strip()
	return value or None
