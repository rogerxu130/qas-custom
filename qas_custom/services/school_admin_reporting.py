from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime_in_timezone, getdate, now_datetime, today

from qas_custom.modules.billing.store_credit import get_invoice_payable_amount, get_invoice_total_amount
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
TRIAL_ATTENDED_STATUSES = {"Present", "Late"}
TRIAL_COUNTABLE_ATTENDANCE_STATUSES = TRIAL_ATTENDED_STATUSES | {"To be started"}
TRIAL_FOLLOWING_UP_STATUSES = {"Completed", "Follow-up"}
TRIAL_FURTHER_TRIAL_BOOKED_STATUS = "Further Trial Booked"
TRIAL_EXCLUDED_INQUIRY_STATUSES = {"No-show"}
TRIAL_TEACHER_UNASSIGNED_LABEL = "No teacher assigned"
BRISBANE_TIMEZONE = "Australia/Brisbane"
DAILY_REPORT_UNMARKED_ATTENDANCE_STATUSES = {"", "Scheduled", "To be started"}
DAILY_REPORT_PHOTO_PREVIEW_LIMIT = 3


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


def get_school_admin_voucher_report_data(status=None, query=None, page=1, page_length=50):
	"""Return the live, voucher-level School Admin report.

	Voucher expiry is calculated at read time because legacy vouchers are not
	automatically rewritten from Valid to Expired when a calendar day passes.
	"""
	_require_school_admin()
	if not _doctype_available("Makeup Voucher"):
		frappe.throw(_("Makeup Voucher data is not installed yet. Please run the site migration."))

	status = str(status or "Usable").strip()
	allowed_statuses = {"Usable", "Used", "Expired", "Cancelled", "All"}
	if status not in allowed_statuses:
		frappe.throw(_("A valid Voucher status filter is required."))
	page = max(1, cint(page) or 1)
	page_length = _page_length(page_length)
	query = str(query or "").strip().lower()

	fields = _safe_fields(
		"Makeup Voucher",
		[
			"name", "student", "course", "original_session", "status", "issue_date",
			"expiry_date", "used_on_session", "used_date", "used_by_student", "redeemed_student",
		],
	)
	vouchers = [dict(row) for row in frappe.get_all("Makeup Voucher", fields=fields, limit_page_length=0)]
	student_ids = sorted({row.get("student") for row in vouchers if row.get("student")})
	students = _student_map(student_ids)
	parent_field = _student_parent_field()
	parent_ids = sorted({(students.get(student) or {}).get(parent_field) for student in student_ids if (students.get(student) or {}).get(parent_field)})
	parents = _parent_map(parent_ids)
	sessions = _voucher_session_map(vouchers)

	items = []
	for voucher in vouchers:
		student = voucher.get("student")
		student_detail = students.get(student) or {}
		parent = student_detail.get(parent_field) if parent_field else None
		parent_detail = parents.get(parent) or {}
		effective_status = _voucher_effective_status(voucher)
		if status != "All" and effective_status != status:
			continue
		search_text = _search_text(
			voucher.get("name"), parent_detail.get("parent_name"), parent_detail.get("email"),
			parent_detail.get("phone"), student, _student_label(student_detail, student),
			voucher.get("course"), _voucher_session_label(sessions.get(voucher.get("original_session"))),
		)
		if query and query not in search_text:
			continue
		items.append(
			{
				"name": voucher.get("name"),
				"status": effective_status,
				"stored_status": voucher.get("status") or "",
				"student": student,
				"student_name": _student_label(student_detail, student),
				"parent_record": parent or "",
				"parent_name": parent_detail.get("parent_name") or "-",
				"parent_email": parent_detail.get("email") or "",
				"parent_phone": parent_detail.get("phone") or "",
				"course": voucher.get("course") or "",
				"original_session": voucher.get("original_session") or "",
				"original_session_label": _voucher_session_label(sessions.get(voucher.get("original_session"))),
				"issue_date": voucher.get("issue_date"),
				"expiry_date": voucher.get("expiry_date"),
				"used_on_session": voucher.get("used_on_session") or "",
				"used_on_session_label": _voucher_session_label(sessions.get(voucher.get("used_on_session"))),
				"used_date": voucher.get("used_date"),
			}
		)

	items.sort(key=_voucher_report_sort_key)
	total = len(items)
	start = (page - 1) * page_length
	return {
		"items": items[start:start + page_length],
		"total": total,
		"page": page,
		"page_length": page_length,
		"has_more": start + page_length < total,
		"options": ["Usable", "Used", "Expired", "Cancelled", "All"],
	}


def get_school_admin_term_paid_invoice_summary_data(term=None):
	"""Return the live count and face-value total of Paid invoices for one Term.

	This is intentionally a small, live financial summary rather than a report
	snapshot.  It counts a Sales Invoice once even when several of its items are
	linked to the selected Term.  Draft, Partly Paid, Unpaid, and Cancelled
	invoices are excluded.
	"""
	_require_school_admin()
	_validate_term(term)
	if not _doctype_available("Sales Invoice"):
		frappe.throw(_("Sales Invoice data is not installed yet. Please run the site migration."))

	invoice_names = _term_invoice_names(term)
	if not invoice_names:
		return {
			"term": term,
			"paid_invoice_count": 0,
			"paid_invoice_total": 0.0,
		}

	fields = _safe_fields(
		"Sales Invoice",
		["name", "docstatus", "status", "grand_total", "rounded_total", "posting_date"],
	)
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", invoice_names], "docstatus": 1, "status": "Paid"},
		fields=fields,
		order_by="posting_date desc, name asc",
		limit_page_length=0,
	)

	paid_total = 0.0
	paid_count = 0
	for row in rows:
		# Keep this defensive guard even though the query filters the rows. It
		# protects the summary from stale/mock rows and future query changes.
		if cint(row.get("docstatus")) != 1 or row.get("status") != "Paid":
			continue
		paid_count += 1
		paid_total += flt(get_invoice_total_amount(frappe._dict(row)))

	return {
		"term": term,
		"paid_invoice_count": paid_count,
		"paid_invoice_total": round(paid_total, 2),
	}


def get_school_admin_active_enrollment_trend_data(term=None):
	"""Return a daily active-enrollment trend for a Term.

	The trend is reconstructed from the Enrollment creation date and real status
	changes recorded in Version.  It deliberately does not infer history from a
	document's ``modified`` timestamp: that would create invented movement dates.
	"""
	_require_school_admin()
	_validate_term(term)
	for doctype in ("Enrollment", "Version"):
		if not _doctype_available(doctype):
			frappe.throw(_("{0} data is not installed yet. Please run the site migration.").format(doctype))

	term_doc = frappe.get_doc("Term", term)
	start_date = getdate(term_doc.start_date)
	end_date = min(getdate(term_doc.end_date), get_datetime_in_timezone(BRISBANE_TIMEZONE).date())
	if end_date < start_date:
		return _empty_active_enrollment_trend(term, start_date, end_date)

	enrollments = frappe.get_all(
		"Enrollment",
		filters={"term": term},
		fields=_safe_fields("Enrollment", ["name", "student", "status", "creation"]),
		order_by="creation asc, name asc",
		limit_page_length=0,
	)
	enrollment_names = [row.get("name") for row in enrollments if row.get("name")]
	versions_by_enrollment = defaultdict(list)
	version_count = 0
	if enrollment_names:
		version_rows = frappe.get_all(
			"Version",
			filters={"ref_doctype": "Enrollment", "docname": ["in", enrollment_names]},
			fields=["name", "docname", "data", "creation"],
			order_by="creation asc, name asc",
			limit_page_length=0,
		)
		version_count = len(version_rows)
		for version in version_rows:
			versions_by_enrollment[version.get("docname")].append(version)

	day_events = defaultdict(list)
	opening_active = 0
	for enrollment in enrollments:
		for event in _active_enrollment_trend_events(enrollment, versions_by_enrollment.get(enrollment.get("name"), [])):
			event_date = event.get("date")
			if event_date and event_date < start_date:
				opening_active += event.get("delta", 0)
			elif event_date:
				day_events[event_date].append(event)

	added_count = 0
	ended_count = 0
	days = []
	active_total = 0
	date_cursor = start_date
	while date_cursor <= end_date:
		day_key = str(date_cursor)
		movements = []
		for event in day_events.get(date_cursor, []):
			if event.get("date") > end_date:
				continue
			movements.append(event)

		if date_cursor == start_date:
			active_total = opening_active
		for event in movements:
			active_total += event.get("delta", 0)
			if event.get("kind") == "added":
				added_count += 1
			elif event.get("kind") == "ended":
				ended_count += 1

		days.append(
			{
				"date": day_key,
				"active_end": active_total,
				"added": sum(1 for event in movements if event.get("kind") == "added"),
				"ended": sum(1 for event in movements if event.get("kind") == "ended"),
				"movements": [
					{
						"kind": event.get("kind"),
						"enrollment": event.get("enrollment"),
						"student": event.get("student") or "-",
						"status": event.get("status") or "",
					}
					for event in movements
				],
			}
		)
		date_cursor += timedelta(days=1)

	return {
		"term": term,
		"range": {"start_date": str(start_date), "end_date": str(end_date)},
		"summary": {
			"opening_active": opening_active,
			"added_count": added_count,
			"ended_count": ended_count,
			"current_active": active_total,
		},
		"days": days,
		"diagnostics": {"enrollment_count": len(enrollments), "version_count": version_count},
	}


def _active_enrollment_trend_events(enrollment, versions):
	"""Return lifecycle movements using only creation and explicit status history."""
	status_changes = []
	for version in versions:
		data = _decode_json(version.get("data"), {})
		changed = data.get("changed") if isinstance(data, dict) else []
		if not isinstance(changed, list):
			continue
		for change in changed:
			if not isinstance(change, (list, tuple)) or len(change) < 3 or change[0] != "status":
				continue
			status_changes.append(
				{
					"date": getdate(version.get("creation")),
					"from_status": change[1] or "",
					"to_status": change[2] or "",
				}
			)

	initial_status = status_changes[0]["from_status"] if status_changes else enrollment.get("status") or ""
	was_active = _is_active_enrollment_trend_status(initial_status)
	events = []
	if was_active:
		events.append(
			{
				"date": getdate(enrollment.get("creation")),
				"kind": "added",
				"delta": 1,
				"enrollment": enrollment.get("name"),
				"student": enrollment.get("student"),
				"status": initial_status,
			}
		)

	for change in status_changes:
		is_active = _is_active_enrollment_trend_status(change["to_status"])
		if was_active and not is_active:
			events.append(
				{
					"date": change["date"],
					"kind": "ended",
					"delta": -1,
					"enrollment": enrollment.get("name"),
					"student": enrollment.get("student"),
					"status": change["to_status"],
				}
			)
		elif not was_active and is_active:
			events.append(
				{
					"date": change["date"],
					"kind": "added",
					"delta": 1,
					"enrollment": enrollment.get("name"),
					"student": enrollment.get("student"),
					"status": change["to_status"],
				}
			)
		was_active = is_active
	return events


def _is_active_enrollment_trend_status(status):
	return str(status or "").strip() not in {"Inactive", "Cancelled"}


def _empty_active_enrollment_trend(term, start_date, end_date):
	return {
		"term": term,
		"range": {"start_date": str(start_date), "end_date": str(end_date)},
		"summary": {"opening_active": 0, "added_count": 0, "ended_count": 0, "current_active": 0},
		"days": [],
		"diagnostics": {"enrollment_count": 0, "version_count": 0},
	}


def get_school_admin_teacher_trial_conversion_report_data(term=None):
	"""Return live, teacher-attributed outcomes for Trials actually attended in one Term."""
	_require_school_admin()
	_validate_term(term)
	for doctype in ("Weekly Timeslot", "Course Sessions", "Class Attendance Entry", "Inquiry"):
		if not _doctype_available(doctype):
			frappe.throw(_("{0} data is not installed yet. Please run the site migration.").format(doctype))

	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": term},
		fields=_safe_fields("Weekly Timeslot", ["name", "teacher"]),
		limit_page_length=0,
	)
	timeslot_map = {row.get("name"): dict(row) for row in timeslots if row.get("name")}
	if not timeslot_map:
		return _empty_teacher_trial_conversion_report(term)

	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": ["in", sorted(timeslot_map)]},
		fields=_safe_fields("Course Sessions", ["name", "weekly_timeslot", "teacher_override", "session_date"]),
		order_by="session_date desc, name asc",
		limit_page_length=0,
	)
	session_map = {row.get("name"): dict(row) for row in sessions if row.get("name")}
	if not session_map:
		return _empty_teacher_trial_conversion_report(term)

	attendance_rows = frappe.get_all(
		"Class Attendance Entry",
		filters={
			"course_session": ["in", sorted(session_map)],
			"enrollment_type": "Trial",
			"source_doctype": "Inquiry",
			"status": ["in", sorted(TRIAL_COUNTABLE_ATTENDANCE_STATUSES)],
		},
		fields=_safe_fields("Class Attendance Entry", ["name", "course_session", "source_document", "status"]),
		order_by="creation asc, name asc",
		limit_page_length=0,
	)
	inquiry_ids = sorted({row.get("source_document") for row in attendance_rows if row.get("source_document")})
	inquiries = {}
	if inquiry_ids:
		inquiries = {
			row.get("name"): dict(row)
			for row in frappe.get_all(
				"Inquiry",
				filters={"name": ["in", inquiry_ids], "inquiry_type": "Trial Lesson"},
				fields=_safe_fields("Inquiry", ["name", "status"]),
				limit_page_length=0,
			)
			if row.get("name")
		}

	teacher_ids = {
		session.get("teacher_override") or (timeslot_map.get(session.get("weekly_timeslot")) or {}).get("teacher")
			for session in session_map.values()
		}
	teacher_ids.discard(None)
	teacher_ids.discard("")
	teacher_names = _teacher_name_map(teacher_ids)
	items = _build_teacher_trial_conversion_items(
		attendance_rows=attendance_rows,
		session_map=session_map,
		timeslot_map=timeslot_map,
		inquiries=inquiries,
		teacher_names=teacher_names,
	)
	return {
		"term": term,
		"summary": {
			"trial_attended_count": sum(row["trial_attended_count"] for row in items),
			"converted_count": sum(row["converted_count"] for row in items),
			"inactive_count": sum(row["inactive_count"] for row in items),
			"following_up_count": sum(row["following_up_count"] for row in items),
			"further_trial_booked_count": sum(row["further_trial_booked_count"] for row in items),
		},
		"items": items,
	}


def get_school_admin_daily_teacher_report_data(session_date=None, campus=None):
	"""Return one live, read-only teaching-quality report for a Brisbane calendar day.

	The report deliberately uses Course Sessions as the row owner. It aggregates all
	attendance and published teacher content in bulk so the client never has to infer
	completeness from a cached classes list or make a request per session.
	"""
	_require_school_admin()
	for doctype in ("Course Sessions", "Weekly Timeslot", "Class Attendance Entry"):
		if not _doctype_available(doctype):
			frappe.throw(_("{0} data is not installed yet. Please run the site migration.").format(doctype))

	target_date = _daily_report_date(session_date)
	campus_options = _daily_report_campus_options()
	campus = str(campus or "").strip()
	if campus and campus not in {row["value"] for row in campus_options}:
		frappe.throw(_("Select a valid campus."))

	sessions = frappe.get_all(
		"Course Sessions",
		filters={"session_date": target_date, "status": ["!=", "Cancelled"]},
		fields=_safe_fields("Course Sessions", ["name", "weekly_timeslot", "teacher_override", "session_date", "status"]),
		order_by="name asc",
		limit_page_length=0,
	)
	if not sessions:
		return _empty_daily_teacher_report(target_date, campus, campus_options)

	timeslots = _daily_report_timeslots(sessions)
	if campus:
		sessions = [
			row for row in sessions if (timeslots.get(row.get("weekly_timeslot")) or {}).get("campus") == campus
		]
	if not sessions:
		return _empty_daily_teacher_report(target_date, campus, campus_options)

	session_ids = sorted({row.get("name") for row in sessions if row.get("name")})
	attendance_rows = frappe.get_all(
		"Class Attendance Entry",
		filters={"course_session": ["in", session_ids]},
		fields=_safe_fields("Class Attendance Entry", ["course_session", "status"]),
		order_by="course_session asc, creation asc",
		limit_page_length=0,
	)
	updates = _daily_report_class_updates(session_ids)
	photo_posts, photo_items = _daily_report_photo_posts(session_ids)
	videos = _daily_report_videos(session_ids)
	teacher_names = _teacher_name_map(
		{
			row.get("teacher_override")
			or (timeslots.get(row.get("weekly_timeslot")) or {}).get("teacher")
			for row in sessions
		}
		| {row.get("teacher") for row in updates + photo_posts + videos}
	)
	items = _build_daily_teacher_report_items(
		sessions=sessions,
		timeslots=timeslots,
		attendance_rows=attendance_rows,
		updates=updates,
		photo_posts=photo_posts,
		photo_items=photo_items,
		videos=videos,
		teacher_names=teacher_names,
	)
	return {
		"session_date": str(target_date),
		"campus": campus,
		"options": {"campuses": campus_options},
		"summary": _daily_teacher_report_summary(items),
		"items": items,
	}


def _daily_report_date(value):
	if not value:
		return get_datetime_in_timezone(BRISBANE_TIMEZONE).date()
	try:
		return getdate(value)
	except Exception:
		frappe.throw(_("Select a valid session date."))


def _daily_report_campus_options():
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"campus": ["!=", ""]},
		fields=["campus"],
		distinct=True,
		limit_page_length=0,
	)
	return [
		{"value": row.get("campus"), "label": row.get("campus")}
		for row in sorted(rows, key=lambda row: str(row.get("campus") or "").lower())
		if row.get("campus")
	]


def _daily_report_timeslots(sessions):
	timeslot_ids = sorted({row.get("weekly_timeslot") for row in sessions if row.get("weekly_timeslot")})
	if not timeslot_ids:
		return {}
	fields = _safe_fields(
		"Weekly Timeslot",
		["name", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
	)
	return {
		row.get("name"): dict(row)
		for row in frappe.get_all(
			"Weekly Timeslot",
			filters={"name": ["in", timeslot_ids]},
			fields=fields,
			limit_page_length=0,
		)
		if row.get("name")
	}


def _daily_report_class_updates(session_ids):
	if not session_ids or not _doctype_available("Session Homework"):
		return []
	return [
		dict(row)
		for row in frappe.get_all(
			"Session Homework",
			filters={"course_session": ["in", session_ids], "status": "Published"},
			fields=_safe_fields("Session Homework", ["name", "course_session", "title", "description", "teacher", "published_at"]),
			order_by="published_at asc, creation asc",
			limit_page_length=0,
		)
	]


def _daily_report_photo_posts(session_ids):
	if not session_ids or not _doctype_available("Session Photo Post"):
		return [], []
	posts = [
		dict(row)
		for row in frappe.get_all(
			"Session Photo Post",
			filters={"course_session": ["in", session_ids], "status": "Published"},
			fields=_safe_fields("Session Photo Post", ["name", "course_session", "title", "caption", "teacher", "posted_at"]),
			order_by="posted_at asc, creation asc",
			limit_page_length=0,
		)
	]
	post_ids = sorted({row.get("name") for row in posts if row.get("name")})
	if not post_ids or not _doctype_available("Session Photo Item"):
		return posts, []
	items = [
		dict(row)
		for row in frappe.get_all(
			"Session Photo Item",
			filters={
				"parent": ["in", post_ids],
				"parenttype": "Session Photo Post",
				"parentfield": "photos",
			},
			fields=["parent", "idx"],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		)
	]
	return posts, items


def _daily_report_videos(session_ids):
	if not session_ids or not _doctype_available("Session Video Post"):
		return []
	return [
		dict(row)
		for row in frappe.get_all(
			"Session Video Post",
			filters={"course_session": ["in", session_ids], "status": "Published"},
			fields=_safe_fields("Session Video Post", ["name", "course_session", "teacher"]),
			limit_page_length=0,
		)
	]


def _build_daily_teacher_report_items(
	*, sessions, timeslots, attendance_rows, updates, photo_posts, photo_items, videos, teacher_names
):
	attendance_by_session = defaultdict(list)
	for row in attendance_rows:
		attendance_by_session[row.get("course_session")].append(row)
	updates_by_session = defaultdict(list)
	for row in updates:
		updates_by_session[row.get("course_session")].append(row)
	posts_by_session = defaultdict(list)
	for row in photo_posts:
		posts_by_session[row.get("course_session")].append(row)
	photos_by_post = defaultdict(list)
	for row in photo_items:
		photos_by_post[row.get("parent")].append(row)
	videos_by_session = defaultdict(list)
	for row in videos:
		videos_by_session[row.get("course_session")].append(row)

	items = []
	for session in sessions:
		session_id = session.get("name")
		timeslot = timeslots.get(session.get("weekly_timeslot")) or {}
		teacher = session.get("teacher_override") or timeslot.get("teacher") or ""
		photo_post_rows = posts_by_session.get(session_id, [])
		photos = []
		photo_post_payloads = []
		for post in photo_post_rows:
			post_photos = [
				_daily_report_photo_payload(session_id, post.get("name"), photo.get("idx"))
				for photo in photos_by_post.get(post.get("name"), [])
			]
			photos.extend(post_photos)
			photo_post_payloads.append(
				{
					"id": post.get("name"),
					"title": post.get("title") or _("Class Photos"),
					"caption": post.get("caption") or "",
					"teacher": post.get("teacher") or "",
					"teacher_name": teacher_names.get(post.get("teacher")) or post.get("teacher") or "",
					"photo_count": len(post_photos),
				}
			)
		attendance = _daily_report_attendance_counts(attendance_by_session.get(session_id, []))
		class_updates = [
			{
				"id": row.get("name"),
				"title": row.get("title") or _("Class Update"),
				"description": row.get("description") or "",
				"teacher": row.get("teacher") or "",
				"teacher_name": teacher_names.get(row.get("teacher")) or row.get("teacher") or "",
			}
			for row in updates_by_session.get(session_id, [])
		]
		items.append(
			{
				"course_session": session_id,
				"course": timeslot.get("course") or _("Class"),
				"campus": timeslot.get("campus") or _("Not assigned"),
				"classroom": timeslot.get("classroom") or _("Not assigned"),
				"teacher": teacher,
				"teacher_name": teacher_names.get(teacher) or teacher or TRIAL_TEACHER_UNASSIGNED_LABEL,
				"start_time": _daily_report_time(timeslot.get("start_time")),
				"end_time": _daily_report_time(timeslot.get("end_time")),
				"attendance": attendance,
				"class_updates": class_updates,
				"photo_post_count": len(photo_post_rows),
				"photo_posts": photo_post_payloads,
				"photo_count": len(photos),
				"photos": photos,
				"photo_preview_limit": DAILY_REPORT_PHOTO_PREVIEW_LIMIT,
				"video_count": len(videos_by_session.get(session_id, [])),
				"needs_attention": {
					"attendance": attendance["unmarked"] > 0,
					"class_update": not class_updates,
					"media": not photos and not videos_by_session.get(session_id),
				},
			}
		)
	items.sort(
		key=lambda row: (
			row.get("start_time") or "",
			row.get("course") or "",
			row.get("campus") or "",
			row.get("course_session") or "",
		)
	)
	return items


def _daily_report_attendance_counts(rows):
	counts = {
		"expected": 0,
		"marked": 0,
		"unmarked": 0,
		"present": 0,
		"absent": 0,
		"late": 0,
		"leave": 0,
		"cancelled": 0,
	}
	for row in rows:
		status = str(row.get("status") or "").strip()
		if status == "Leave":
			counts["leave"] += 1
			continue
		if status == "Cancelled":
			counts["cancelled"] += 1
			continue
		counts["expected"] += 1
		if status in DAILY_REPORT_UNMARKED_ATTENDANCE_STATUSES:
			counts["unmarked"] += 1
		else:
			counts["marked"] += 1
		if status == "Present":
			counts["present"] += 1
		elif status == "Absent":
			counts["absent"] += 1
		elif status == "Late":
			counts["late"] += 1
	return counts


def _daily_report_photo_payload(course_session, photo_post, photo_idx):
	params = {"course_session": course_session, "photo_post": photo_post, "photo_idx": cint(photo_idx)}
	return {
		"photo_post": photo_post,
		"idx": cint(photo_idx),
		"preview_url": _daily_report_media_url("school_admin_get_course_session_photo_preview", params),
		"full_url": _daily_report_media_url("school_admin_get_course_session_photo", params),
	}


def _daily_report_media_url(method, params):
	return "/api/method/qas_custom.api.school_admin.{0}?{1}".format(method, urlencode(params))


def _daily_report_time(value):
	text = str(value or "").strip()
	return text[:5] if len(text) >= 5 else text or "-"


def _daily_teacher_report_summary(items):
	return {
		"session_count": len(items),
		"incomplete_attendance_count": sum(1 for row in items if row["needs_attention"]["attendance"]),
		"missing_class_update_count": sum(1 for row in items if row["needs_attention"]["class_update"]),
		"missing_media_count": sum(1 for row in items if row["needs_attention"]["media"]),
		"photo_count": sum(cint(row.get("photo_count")) for row in items),
	}


def _empty_daily_teacher_report(target_date, campus, campus_options):
	return {
		"session_date": str(target_date),
		"campus": campus,
		"options": {"campuses": campus_options},
		"summary": _daily_teacher_report_summary([]),
		"items": [],
	}


def _empty_teacher_trial_conversion_report(term):
	return {
		"term": term,
		"summary": {
			"trial_attended_count": 0,
			"converted_count": 0,
			"inactive_count": 0,
			"following_up_count": 0,
			"further_trial_booked_count": 0,
		},
		"items": [],
	}


def _teacher_name_map(teacher_ids):
	if not teacher_ids or not _doctype_available("Teacher"):
		return {}
	fields = _safe_fields("Teacher", ["name", "teacher_name"])
	return {
		row.get("name"): row.get("teacher_name") or row.get("name")
		for row in frappe.get_all(
			"Teacher",
			filters={"name": ["in", sorted(teacher_ids)]},
			fields=fields,
			limit_page_length=0,
		)
		if row.get("name")
	}


def _build_teacher_trial_conversion_items(*, attendance_rows, session_map, timeslot_map, inquiries, teacher_names):
	grouped = defaultdict(
		lambda: {
			"trial_attended_count": 0,
			"converted_count": 0,
			"inactive_count": 0,
			"following_up_count": 0,
			"further_trial_booked_count": 0,
		}
	)
	seen_inquiries = set()
	for attendance in attendance_rows:
		if attendance.get("status") not in TRIAL_COUNTABLE_ATTENDANCE_STATUSES:
			continue
		inquiry_id = attendance.get("source_document")
		inquiry = inquiries.get(inquiry_id) or {}
		session = session_map.get(attendance.get("course_session")) or {}
		timeslot = timeslot_map.get(session.get("weekly_timeslot")) or {}
		if (
			not inquiry_id
			or not inquiry
			or not session
			or inquiry_id in seen_inquiries
			or inquiry.get("status") in TRIAL_EXCLUDED_INQUIRY_STATUSES
			or not _is_countable_trial_attendance(attendance, session, timeslot)
		):
			continue
		seen_inquiries.add(inquiry_id)
		teacher = session.get("teacher_override") or timeslot.get("teacher") or ""
		row = grouped[teacher]
		row["trial_attended_count"] += 1
		if inquiry.get("status") == "Converted":
			row["converted_count"] += 1
		elif inquiry.get("status") == "Inactive":
			row["inactive_count"] += 1
		elif inquiry.get("status") == TRIAL_FURTHER_TRIAL_BOOKED_STATUS:
			row["further_trial_booked_count"] += 1
		elif inquiry.get("status") in TRIAL_FOLLOWING_UP_STATUSES:
			row["following_up_count"] += 1

	items = []
	for teacher, counts in grouped.items():
		items.append(
			{
				"teacher": teacher,
				"teacher_name": teacher_names.get(teacher) or teacher or TRIAL_TEACHER_UNASSIGNED_LABEL,
				**counts,
			}
		)
	return sorted(items, key=lambda row: (-row["trial_attended_count"], row["teacher_name"].lower(), row["teacher"]))


def _is_countable_trial_attendance(attendance, session, timeslot):
	"""Include forgotten attendance marks only after the Trial session has passed."""
	if attendance.get("status") in TRIAL_ATTENDED_STATUSES:
		return True
	if attendance.get("status") != "To be started" or not session.get("session_date"):
		return False
	return _session_end_datetime(session, timeslot) < now_datetime()


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


def _voucher_session_map(vouchers):
	session_ids = sorted(
		{
			session_id
			for voucher in vouchers
			for session_id in (voucher.get("original_session"), voucher.get("used_on_session"))
			if session_id
		}
	)
	if not session_ids:
		return {}
	session_fields = _safe_fields("Course Sessions", ["name", "weekly_timeslot", "session_date", "status"])
	sessions = {
		row.get("name"): dict(row)
		for row in frappe.get_all("Course Sessions", filters={"name": ["in", session_ids]}, fields=session_fields, limit_page_length=0)
	}
	timeslot_ids = sorted({row.get("weekly_timeslot") for row in sessions.values() if row.get("weekly_timeslot")})
	if not timeslot_ids:
		return sessions
	timeslot_fields = _safe_fields("Weekly Timeslot", ["name", "course", "campus", "start_time", "end_time"])
	timeslots = {
		row.get("name"): dict(row)
		for row in frappe.get_all("Weekly Timeslot", filters={"name": ["in", timeslot_ids]}, fields=timeslot_fields, limit_page_length=0)
	}
	for session in sessions.values():
		session["timeslot"] = timeslots.get(session.get("weekly_timeslot")) or {}
	return sessions


def _voucher_effective_status(voucher):
	stored_status = voucher.get("status") or ""
	if stored_status == "Valid" and voucher.get("expiry_date") and getdate(voucher.get("expiry_date")) < getdate(today()):
		return "Expired"
	if stored_status == "Valid":
		return "Usable"
	return stored_status or "Cancelled"


def _voucher_session_label(session):
	if not session:
		return ""
	timeslot = session.get("timeslot") or {}
	parts = [session.get("session_date"), timeslot.get("start_time"), timeslot.get("course"), timeslot.get("campus")]
	return " · ".join(str(value) for value in parts if value not in (None, "")) or session.get("name") or ""


def _voucher_report_sort_key(row):
	status_rank = {"Usable": 0, "Used": 1, "Expired": 2, "Cancelled": 3}.get(row.get("status"), 4)
	expiry = str(row.get("expiry_date") or "9999-12-31")
	issue = str(row.get("issue_date") or "")
	return status_rank, expiry, issue, row.get("name") or ""


def _term_invoice_map(term, parent_ids, parents):
	if not parent_ids or not _doctype_available("Sales Invoice"):
		return {}
	invoice_names = _term_invoice_names(term)
	if not invoice_names:
		return {}

	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "parent", "docstatus", "status", "grand_total", "rounded_total", "outstanding_amount"],
	)
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", invoice_names], "docstatus": ["!=", 2]},
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


def _term_invoice_names(term):
	"""Find unique invoice IDs that are linked to a Term at either level."""
	if not _doctype_available("Sales Invoice"):
		return []

	invoice_names = set()
	if _has_field("Sales Invoice", "term"):
		invoice_names.update(
			frappe.get_all(
				"Sales Invoice",
				filters={"term": term},
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
	return sorted(name for name in invoice_names if name)


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
	_validate_term(term)


def _validate_term(term):
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
