from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

from qas_custom.modules.billing.store_credit import get_invoice_payable_amount
from qas_custom.services.display_labels import get_student_display_name


SNAPSHOT_DOCTYPE = "QAS Admin Report Snapshot"
ROW_DOCTYPE = "QAS Admin Report Row"
FAMILY_REPORT_TYPE = "Family Summary"
UNMARKED_REPORT_TYPE = "Unmarked Attendance"
ELIGIBLE_ENROLLMENT_STATUSES = ("Active", "Planned", "Completed")
RUNNING_STATUSES = ("Queued", "Running")
REPORT_TYPES = (FAMILY_REPORT_TYPE, UNMARKED_REPORT_TYPE)
ADMIN_ROLES = {"School Admin", "System Manager"}
PAGE_LENGTH_MAX = 200


def get_school_admin_reporting_snapshot_data(term=None):
	_require_school_admin()
	_validate_reporting_term(term)
	latest = _latest_completed_snapshot(term)
	generation = _latest_generation(term)
	return {
		"term": term,
		"latest": _snapshot_payload(latest),
		"generation": _snapshot_payload(generation),
	}


def start_school_admin_reporting_generation_data(term=None):
	_require_school_admin()
	_validate_reporting_term(term)
	_assert_reporting_doctypes()
	existing = _running_snapshot(term)
	if existing:
		return {"queued": False, "reused": True, "snapshot": _snapshot_payload(existing)}

	doc = frappe.new_doc(SNAPSHOT_DOCTYPE)
	doc.term = term
	doc.status = "Queued"
	doc.is_latest = 0
	doc.requested_by = frappe.session.user
	doc.requested_at = now_datetime()
	doc.insert(ignore_permissions=True)
	frappe.enqueue(
		"qas_custom.services.school_admin_reporting.run_school_admin_reporting_generation_job",
		queue="long",
		timeout=1800,
		enqueue_after_commit=True,
		job_id=f"qas-school-admin-reporting:{term}",
		deduplicate=True,
		snapshot=doc.name,
	)
	frappe.db.commit()
	return {"queued": True, "reused": False, "snapshot": _snapshot_payload(doc)}


def run_school_admin_reporting_generation_job(snapshot=None):
	if not snapshot or not frappe.db.exists(SNAPSHOT_DOCTYPE, snapshot):
		return {"completed": False, "reason": "Snapshot was not found."}

	doc = frappe.get_doc(SNAPSHOT_DOCTYPE, snapshot)
	if doc.status == "Completed":
		return {"completed": True, "snapshot": doc.name, "duplicate": True}

	doc.status = "Running"
	doc.started_at = now_datetime()
	doc.failure_reason = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		generated_at = now_datetime()
		result = _build_reporting_rows(doc.term, generated_at)
		frappe.db.savepoint("qas_admin_reporting_rows")
		for values in result["family_rows"] + result["unmarked_rows"]:
			row = frappe.new_doc(ROW_DOCTYPE)
			row.update(values)
			row.snapshot = doc.name
			row.term = doc.term
			row.insert(ignore_permissions=True)

		_previous_latest = frappe.get_all(
			SNAPSHOT_DOCTYPE,
			filters={"term": doc.term, "is_latest": 1, "name": ["!=", doc.name]},
			pluck="name",
			limit_page_length=0,
		)
		for previous in _previous_latest:
			frappe.db.set_value(SNAPSHOT_DOCTYPE, previous, "is_latest", 0, update_modified=False)

		doc.reload()
		doc.status = "Completed"
		doc.is_latest = 1
		doc.completed_at = generated_at
		doc.family_row_count = len(result["family_rows"])
		doc.unmarked_row_count = len(result["unmarked_rows"])
		doc.skipped_count = result["skipped_count"]
		doc.failure_reason = None
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		try:
			_cleanup_superseded_snapshots(doc.term, keep=3)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"QAS reporting snapshot cleanup failed: {doc.term}")
		return {
			"completed": True,
			"snapshot": doc.name,
			"family_row_count": doc.family_row_count,
			"unmarked_row_count": doc.unmarked_row_count,
		}
	except Exception as exc:
		frappe.db.rollback()
		failure_reason = _safe_failure_reason(exc)
		if frappe.db.exists(SNAPSHOT_DOCTYPE, snapshot):
			frappe.db.set_value(
				SNAPSHOT_DOCTYPE,
				snapshot,
				{
					"status": "Failed",
					"is_latest": 0,
					"completed_at": now_datetime(),
					"failure_reason": failure_reason,
				},
				update_modified=True,
			)
			frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), f"QAS School Admin reporting generation failed: {snapshot}")
		return {"completed": False, "snapshot": snapshot, "reason": failure_reason}


def get_school_admin_reporting_rows_data(
	term=None,
	report_type=None,
	attendance=None,
	invoice=None,
	campus=None,
	teacher=None,
	query=None,
	page=1,
	page_length=50,
):
	_require_school_admin()
	_validate_reporting_term(term)
	if report_type not in REPORT_TYPES:
		frappe.throw(_("A valid report type is required."))
	snapshot = _latest_completed_snapshot(term)
	if not snapshot:
		return {"snapshot": None, "items": [], "total": 0, "page": 1, "page_length": _page_length(page_length)}

	filters = {"snapshot": snapshot.name, "report_type": report_type}
	if report_type == FAMILY_REPORT_TYPE and attendance:
		filters["attendance_classification"] = attendance
	if invoice:
		filters["invoice_classification"] = invoice
	if report_type == UNMARKED_REPORT_TYPE and campus:
		filters["campus"] = campus
	if report_type == UNMARKED_REPORT_TYPE and teacher:
		filters["teacher"] = teacher
	query = str(query or "").strip()
	if query:
		filters["search_text"] = ["like", f"%{query}%"]

	page = max(1, cint(page) or 1)
	page_length = _page_length(page_length)
	total = frappe.db.count(ROW_DOCTYPE, filters=filters)
	fields = _row_fields(report_type)
	rows = frappe.get_all(
		ROW_DOCTYPE,
		filters=filters,
		fields=fields,
		order_by=_row_order(report_type),
		limit_start=(page - 1) * page_length,
		limit_page_length=page_length,
	)
	return {
		"snapshot": _snapshot_payload(snapshot),
		"items": [_report_row_payload(row) for row in rows],
		"total": total,
		"page": page,
		"page_length": page_length,
		"has_more": page * page_length < total,
		"options": _report_filter_options(snapshot.name, report_type),
	}


def get_school_admin_reporting_family_detail_data(row=None):
	_require_school_admin()
	if not row or not frappe.db.exists(ROW_DOCTYPE, {"name": row, "report_type": FAMILY_REPORT_TYPE}):
		frappe.throw(_("Reporting family row was not found."))
	values = frappe.db.get_value(
		ROW_DOCTYPE,
		row,
		["name", "snapshot", "parent_record", "student_details_json", "invoice_names_json"],
		as_dict=True,
	)
	latest = frappe.db.get_value(SNAPSHOT_DOCTYPE, values.snapshot, "is_latest")
	if not cint(latest):
		frappe.throw(_("This report snapshot is no longer current. Please refresh the report."))
	return {
		"row": values.name,
		"snapshot": values.snapshot,
		"parent": values.parent_record,
		"students": _decode_json(values.student_details_json, []),
		"invoices": _decode_json(values.invoice_names_json, []),
	}


def _build_reporting_rows(term, generated_at):
	term_doc = frappe.get_doc("Term", term)
	term_start = getdate(term_doc.start_date)
	term_end = min(getdate(term_doc.end_date), generated_at.date())
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"term": term, "status": ["in", list(ELIGIBLE_ENROLLMENT_STATUSES)]},
		fields=_safe_fields("Enrollment", ["name", "student", "parent", "weekly_timeslot", "course", "status"]),
		limit_page_length=0,
	)
	student_ids = sorted({row.get("student") for row in enrollments if row.get("student")})
	students = _student_map(student_ids)
	parent_field = _student_parent_field()
	families = defaultdict(lambda: {"students": set(), "enrollments": set()})
	skipped_count = 0
	for enrollment in enrollments:
		student = enrollment.get("student")
		parent = enrollment.get("parent") or (students.get(student) or {}).get(parent_field)
		if not student or not parent:
			skipped_count += 1
			continue
		families[parent]["students"].add(student)
		families[parent]["enrollments"].add(enrollment.get("name"))

	parent_ids = sorted(families)
	parents = _parent_map(parent_ids)
	attendance = _attendance_rows(enrollments)
	sessions, timeslots = _session_context(attendance)
	invoice_map = _term_invoice_map(term, parent_ids, parents)

	attendance_by_parent = defaultdict(list)
	student_parent = {
		student: parent
		for parent, family in families.items()
		for student in family["students"]
	}
	for row in attendance:
		parent = student_parent.get(row.get("student"))
		session = sessions.get(row.get("course_session"))
		if not parent or not _session_in_completed_range(session, timeslots, term_start, term_end, generated_at):
			continue
		attendance_by_parent[parent].append(row)

	family_rows = []
	for parent in parent_ids:
		family = families[parent]
		family_attendance = attendance_by_parent.get(parent, [])
		counts = _attendance_counts(family_attendance)
		student_details = []
		for student in sorted(family["students"], key=lambda item: _student_label(students.get(item), item).lower()):
			student_rows = [row for row in family_attendance if row.get("student") == student]
			student_counts = _attendance_counts(student_rows)
			student_details.append(
				{
					"student": student,
					"student_name": _student_label(students.get(student), student),
					"attendance_classification": _attendance_classification(student_counts),
					**student_counts,
				}
			)
		parent_detail = parents.get(parent) or {"name": parent, "parent_name": parent}
		invoice_detail = invoice_map.get(parent) or _empty_invoice_summary()
		student_names = [item["student_name"] for item in student_details]
		family_student_ids = sorted(family["students"])
		family_rows.append(
			{
				"report_type": FAMILY_REPORT_TYPE,
				"parent_record": parent,
				"parent_name": parent_detail.get("parent_name") or parent,
				"parent_email": parent_detail.get("email") or "",
				"parent_phone": parent_detail.get("phone") or "",
				"attendance_classification": _attendance_classification(counts),
				**counts,
				"invoice_classification": invoice_detail["classification"],
				"outstanding_amount": invoice_detail["outstanding_amount"],
				"invoice_names_json": json.dumps(invoice_detail["invoices"], default=str),
				"student_details_json": json.dumps(student_details, default=str),
				"search_text": _search_text(
					parent,
					parent_detail.get("parent_name"),
					parent_detail.get("email"),
					parent_detail.get("phone"),
					*family_student_ids,
					*student_names,
				),
			}
		)

	window_start = generated_at.date() - timedelta(days=6)
	unmarked_rows = []
	for row in attendance:
		if row.get("status") != "To be started":
			continue
		session = sessions.get(row.get("course_session"))
		if not _session_in_unmarked_window(session, timeslots, term_start, term_end, window_start, generated_at):
			continue
		student = row.get("student")
		parent = student_parent.get(student)
		if not parent:
			continue
		parent_detail = parents.get(parent) or {"name": parent, "parent_name": parent}
		student_name = _student_label(students.get(student), student)
		invoice_detail = invoice_map.get(parent) or _empty_invoice_summary()
		timeslot = timeslots.get(session.get("weekly_timeslot")) or {}
		end_datetime = _session_end_datetime(session, timeslot)
		teacher = session.get("teacher_override") or timeslot.get("teacher")
		class_label = session.get("weekly_timeslot") or row.get("course_session")
		unmarked_rows.append(
			{
				"report_type": UNMARKED_REPORT_TYPE,
				"parent_record": parent,
				"parent_name": parent_detail.get("parent_name") or parent,
				"parent_email": parent_detail.get("email") or "",
				"parent_phone": parent_detail.get("phone") or "",
				"student": student,
				"student_name": student_name,
				"attendance_entry": row.get("name"),
				"course_session": row.get("course_session"),
				"session_date": session.get("session_date"),
				"session_start_time": timeslot.get("start_time"),
				"session_end_time": timeslot.get("end_time") or timeslot.get("start_time"),
				"overdue_days": max(0, (generated_at.date() - end_datetime.date()).days),
				"campus": timeslot.get("campus"),
				"course": timeslot.get("course"),
				"weekly_timeslot": session.get("weekly_timeslot"),
				"teacher": teacher,
				"class_label": class_label,
				"invoice_classification": invoice_detail["classification"],
				"outstanding_amount": invoice_detail["outstanding_amount"],
				"invoice_names_json": json.dumps(invoice_detail["invoices"], default=str),
				"search_text": _search_text(
					parent,
					parent_detail.get("parent_name"),
					parent_detail.get("email"),
					parent_detail.get("phone"),
					student,
					student_name,
					timeslot.get("course"),
					class_label,
				),
			}
		)

	return {"family_rows": family_rows, "unmarked_rows": unmarked_rows, "skipped_count": skipped_count}


def _student_map(student_ids):
	if not student_ids:
		return {}
	fields = _safe_fields("Student", ["name", "student_name", "first_name", "last_name", "guardian", "parent"])
	return {row.get("name"): dict(row) for row in frappe.get_all("Student", filters={"name": ["in", student_ids]}, fields=fields, limit_page_length=0)}


def _parent_map(parent_ids):
	if not parent_ids:
		return {}
	fields = _safe_fields(
		"Parent",
		["name", "parent_name", "email", "email_id", "contact_email", "mobile_number", "phone", "customer"],
	)
	result = {}
	for row in frappe.get_all("Parent", filters={"name": ["in", parent_ids]}, fields=fields, limit_page_length=0):
		result[row.get("name")] = {
			"name": row.get("name"),
			"parent_name": row.get("parent_name") or row.get("name"),
			"email": row.get("email") or row.get("email_id") or row.get("contact_email") or "",
			"phone": row.get("mobile_number") or row.get("phone") or "",
			"customer": row.get("customer"),
		}
	return result


def _attendance_rows(enrollments):
	enrollment_names = sorted({row.get("name") for row in enrollments if row.get("name")})
	if not enrollment_names:
		return []
	return frappe.get_all(
		"Class Attendance Entry",
		filters={"source_doctype": "Enrollment", "source_document": ["in", enrollment_names]},
		fields=["name", "source_document", "student", "status", "course_session"],
		limit_page_length=0,
	)


def _session_context(attendance):
	session_ids = sorted({row.get("course_session") for row in attendance if row.get("course_session")})
	if not session_ids:
		return {}, {}
	session_fields = _safe_fields(
		"Course Sessions",
		["name", "weekly_timeslot", "session_date", "status", "teacher_override"],
	)
	sessions = {
		row.get("name"): dict(row)
		for row in frappe.get_all(
			"Course Sessions",
			filters={"name": ["in", session_ids]},
			fields=session_fields,
			limit_page_length=0,
		)
	}
	timeslot_ids = sorted({row.get("weekly_timeslot") for row in sessions.values() if row.get("weekly_timeslot")})
	timeslots = {}
	if timeslot_ids:
		fields = _safe_fields(
			"Weekly Timeslot",
			["name", "term", "course", "campus", "teacher", "start_time", "end_time", "status"],
		)
		timeslots = {
			row.get("name"): dict(row)
			for row in frappe.get_all(
				"Weekly Timeslot",
				filters={"name": ["in", timeslot_ids]},
				fields=fields,
				limit_page_length=0,
			)
		}
	return sessions, timeslots


def _term_invoice_map(term, parent_ids, parents):
	if not parent_ids or not _doctype_available("Sales Invoice"):
		return {}
	invoice_names = set()
	if _has_field("Sales Invoice", "term"):
		invoice_names.update(
			frappe.get_all(
				"Sales Invoice",
				filters={"term": term, "docstatus": ["!=", 2]},
				pluck="name",
				limit_page_length=0,
			)
		)
	if _doctype_available("Sales Invoice Item") and _has_field("Sales Invoice Item", "term"):
		filters = {"term": term}
		if _has_field("Sales Invoice Item", "parenttype"):
			filters["parenttype"] = "Sales Invoice"
		invoice_names.update(
			frappe.get_all("Sales Invoice Item", filters=filters, pluck="parent", limit_page_length=0)
		)
	if not invoice_names:
		return {}

	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "parent", "docstatus", "status", "grand_total", "rounded_total", "outstanding_amount"],
	)
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", sorted(invoice_names)], "docstatus": ["!=", 2]},
		fields=fields,
		limit_page_length=0,
	)
	customer_parents = defaultdict(set)
	for parent, detail in parents.items():
		if detail.get("customer"):
			customer_parents[detail["customer"]].add(parent)

	grouped = defaultdict(list)
	for row in rows:
		targets = {row.get("parent")} if row.get("parent") in parent_ids else customer_parents.get(row.get("customer"), set())
		for parent in targets:
			if parent in parent_ids:
				grouped[parent].append(row)
	return {parent: _invoice_summary(rows) for parent, rows in grouped.items()}


def _invoice_summary(rows):
	outstanding = 0.0
	has_submitted = False
	has_draft = False
	invoices = []
	for row in rows:
		docstatus = cint(row.get("docstatus"))
		payable = flt(get_invoice_payable_amount(frappe._dict(row)))
		if docstatus == 1:
			has_submitted = True
			outstanding += max(0, payable)
		elif docstatus == 0:
			has_draft = True
		invoices.append(
			{
				"name": row.get("name"),
				"status": row.get("status") or ("Draft" if docstatus == 0 else "Submitted"),
				"docstatus": docstatus,
				"payable_amount": payable,
			}
		)
	if outstanding > 0.005:
		classification = "Outstanding"
	elif has_draft:
		classification = "Draft Invoice"
	elif has_submitted:
		classification = "Not Outstanding"
	else:
		classification = "No Invoice"
	return {"classification": classification, "outstanding_amount": outstanding, "invoices": invoices}


def _empty_invoice_summary():
	return {"classification": "No Invoice", "outstanding_amount": 0, "invoices": []}


def _attendance_counts(rows):
	counts = {"present_late_count": 0, "absent_count": 0, "leave_count": 0, "cancelled_count": 0, "attendance_total": 0}
	for row in rows:
		status = row.get("status")
		if status in {"Present", "Late"}:
			counts["present_late_count"] += 1
		elif status == "Absent":
			counts["absent_count"] += 1
		elif status == "Leave":
			counts["leave_count"] += 1
		elif status == "Cancelled":
			counts["cancelled_count"] += 1
		else:
			continue
		counts["attendance_total"] += 1
	return counts


def _attendance_classification(counts):
	if counts.get("present_late_count"):
		return "Attended"
	if counts.get("absent_count"):
		return "Absent"
	if counts.get("leave_count"):
		return "Leave"
	if counts.get("cancelled_count"):
		return "Cancelled only"
	return "No attendance records"


def _session_in_completed_range(session, timeslots, term_start, term_end, generated_at):
	if not session or session.get("status") == "Cancelled" or not session.get("session_date"):
		return False
	session_date = getdate(session.get("session_date"))
	if session_date < term_start or session_date > term_end:
		return False
	timeslot = timeslots.get(session.get("weekly_timeslot")) or {}
	return _session_end_datetime(session, timeslot) <= generated_at


def _session_in_unmarked_window(session, timeslots, term_start, term_end, window_start, generated_at):
	if not _session_in_completed_range(session, timeslots, term_start, term_end, generated_at):
		return False
	return getdate(session.get("session_date")) >= window_start


def _session_end_datetime(session, timeslot):
	session_date = getdate(session.get("session_date"))
	value = timeslot.get("end_time") or timeslot.get("start_time") or time.min
	return datetime.combine(session_date, _time_value(value))


def _time_value(value):
	if isinstance(value, time):
		return value
	if isinstance(value, timedelta):
		seconds = int(value.total_seconds()) % 86400
		return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
	if isinstance(value, str) and value:
		for pattern in ("%H:%M:%S", "%H:%M"):
			try:
				return datetime.strptime(value, pattern).time()
			except ValueError:
				continue
	return time.min


def _latest_completed_snapshot(term):
	rows = frappe.get_all(
		SNAPSHOT_DOCTYPE,
		filters={"term": term, "status": "Completed", "is_latest": 1},
		fields=_snapshot_fields(),
		order_by="completed_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _latest_generation(term):
	rows = frappe.get_all(
		SNAPSHOT_DOCTYPE,
		filters={"term": term},
		fields=_snapshot_fields(),
		order_by="requested_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _running_snapshot(term):
	rows = frappe.get_all(
		SNAPSHOT_DOCTYPE,
		filters={"term": term, "status": ["in", list(RUNNING_STATUSES)]},
		fields=_snapshot_fields(),
		order_by="requested_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _snapshot_fields():
	return [
		"name",
		"term",
		"status",
		"is_latest",
		"requested_by",
		"requested_at",
		"started_at",
		"completed_at",
		"family_row_count",
		"unmarked_row_count",
		"skipped_count",
		"failure_reason",
	]


def _snapshot_payload(snapshot):
	if not snapshot:
		return None
	return {field: snapshot.get(field) for field in _snapshot_fields()}


def _row_fields(report_type):
	common = [
		"name",
		"snapshot",
		"report_type",
		"term",
		"parent_record",
		"parent_name",
		"parent_email",
		"parent_phone",
		"invoice_classification",
		"outstanding_amount",
		"invoice_names_json",
	]
	if report_type == FAMILY_REPORT_TYPE:
		return common + [
			"attendance_classification",
			"present_late_count",
			"absent_count",
			"leave_count",
			"cancelled_count",
			"attendance_total",
			"student_details_json",
		]
	return common + [
		"student",
		"student_name",
		"attendance_entry",
		"course_session",
		"session_date",
		"session_start_time",
		"session_end_time",
		"overdue_days",
		"campus",
		"course",
		"weekly_timeslot",
		"teacher",
		"class_label",
	]


def _report_row_payload(row):
	payload = dict(row)
	payload["invoices"] = _decode_json(payload.pop("invoice_names_json", None), [])
	if "student_details_json" in payload:
		payload["students"] = _decode_json(payload.pop("student_details_json", None), [])
	payload["outstanding_amount"] = flt(payload.get("outstanding_amount"))
	return payload


def _report_filter_options(snapshot, report_type):
	filters = {"snapshot": snapshot, "report_type": report_type}
	result = {"campuses": [], "teachers": []}
	if report_type == UNMARKED_REPORT_TYPE:
		result["campuses"] = sorted(set(frappe.get_all(ROW_DOCTYPE, filters=filters, pluck="campus", limit_page_length=0)) - {None, ""})
		result["teachers"] = sorted(set(frappe.get_all(ROW_DOCTYPE, filters=filters, pluck="teacher", limit_page_length=0)) - {None, ""})
	return result


def _row_order(report_type):
	return "parent_name asc, name asc" if report_type == FAMILY_REPORT_TYPE else "session_date desc, campus asc, teacher asc, student_name asc"


def _cleanup_superseded_snapshots(term, keep=3):
	rows = frappe.get_all(
		SNAPSHOT_DOCTYPE,
		filters={"term": term, "status": "Completed"},
		pluck="name",
		order_by="completed_at desc, creation desc",
		limit_page_length=0,
	)
	for snapshot in rows[keep:]:
		frappe.db.delete(ROW_DOCTYPE, {"snapshot": snapshot})
		frappe.delete_doc(SNAPSHOT_DOCTYPE, snapshot, ignore_permissions=True, force=True)
	frappe.db.commit()


def _validate_reporting_term(term):
	_assert_reporting_doctypes()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term {0} was not found.").format(term))


def _assert_reporting_doctypes():
	for doctype in (SNAPSHOT_DOCTYPE, ROW_DOCTYPE):
		if not _doctype_available(doctype):
			frappe.throw(_("Reporting data is not installed yet. Please run the site migration."))


def _require_school_admin():
	if not set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)


def _student_parent_field():
	for fieldname in ("guardian", "parent"):
		if _has_field("Student", fieldname):
			return fieldname
	return None


def _student_label(row, fallback):
	if row:
		return row.get("student_name") or " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or fallback
	return get_student_display_name(fallback) or fallback


def _safe_fields(doctype, fields):
	return [field for field in fields if field == "name" or _has_field(doctype, field)] or ["name"]


def _has_field(doctype, fieldname):
	if fieldname in {"name", "owner", "creation", "modified", "docstatus"}:
		return True
	return _doctype_available(doctype) and frappe.get_meta(doctype).has_field(fieldname)


def _doctype_available(doctype):
	try:
		return bool(frappe.db.exists("DocType", doctype)) and bool(frappe.db.table_exists(doctype))
	except Exception:
		return False


def _page_length(value):
	return max(1, min(cint(value) or 50, PAGE_LENGTH_MAX))


def _decode_json(value, fallback):
	if not value:
		return fallback
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return fallback


def _search_text(*values):
	return " ".join(str(value).strip().lower() for value in values if value not in (None, ""))


def _safe_failure_reason(exc):
	message = str(exc or "").strip()
	return message[:500] if message else "Report generation failed."
