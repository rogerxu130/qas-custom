from __future__ import annotations

import frappe
from frappe.utils import cint, flt, getdate


DEFAULT_REVENUE_SHARE_PERCENT = 2
REVENUE_ATTENDANCE_STATUSES = {"To be started", "Present", "Absent", "Late", "Leave"}


def get_teacher_revenue_share_session_rows(
	from_date=None,
	to_date=None,
	teacher=None,
	campus=None,
	course=None,
	owned_only=1,
	limit=200,
):
	if not _doctype_available("Course Sessions"):
		return []

	sessions = _get_sessions(from_date=from_date, to_date=to_date, campus=campus, course=course, limit=limit)
	if not sessions:
		return []

	timeslot_map = _get_timeslot_map([row.get("weekly_timeslot") for row in sessions])
	course_map = _get_course_map(
		[
			(timeslot_map.get(row.get("weekly_timeslot")) or {}).get("course")
			for row in sessions
			if row.get("weekly_timeslot")
		]
	)
	attendance_map = _get_attendance_map([row.get("name") for row in sessions])

	items = []
	for session in sessions:
		timeslot = timeslot_map.get(session.get("weekly_timeslot")) or {}
		settings = get_teacher_revenue_share_settings(session, timeslot)
		if cint(owned_only) and not settings["revenue_share_enabled"]:
			continue
		if teacher and settings.get("revenue_share_teacher") != teacher:
			continue

		course_doc = course_map.get(timeslot.get("course")) or {}
		revenue = estimate_session_revenue(attendance_map.get(session.get("name"), []), course_doc)
		items.append(
			{
				"course_session": session.get("name"),
				"session_date": session.get("session_date"),
				"status": session.get("status"),
				"weekly_timeslot": session.get("weekly_timeslot"),
				"term": timeslot.get("term"),
				"course": timeslot.get("course"),
				"campus": timeslot.get("campus"),
				"classroom": timeslot.get("classroom"),
				"scheduled_teacher": timeslot.get("teacher"),
				"day_of_week": timeslot.get("day_of_week"),
				"start_time": timeslot.get("start_time"),
				"end_time": timeslot.get("end_time"),
				**settings,
				**revenue,
				"teacher_share_amount": round(
					revenue["estimated_gross_revenue"] * settings["revenue_share_percent"] / 100,
					2,
				)
				if settings["revenue_share_enabled"]
				else 0,
			}
		)
	return items


def get_teacher_revenue_share_settings(session, timeslot=None):
	session = _rowdict(session)
	timeslot = _rowdict(timeslot) if timeslot else _get_timeslot(session.get("weekly_timeslot"))
	override = session.get("revenue_share_override") or "Inherit from Weekly Timeslot"

	if override == "Teacher-Owned":
		enabled = True
	elif override == "Not Teacher-Owned":
		enabled = False
	else:
		enabled = bool(cint(timeslot.get("revenue_share_enabled")))

	teacher = session.get("revenue_share_teacher") or timeslot.get("revenue_share_teacher")
	if enabled and not teacher:
		teacher = timeslot.get("teacher")

	percent = session.get("revenue_share_percent")
	if percent in (None, ""):
		percent = timeslot.get("revenue_share_percent")
	if percent in (None, ""):
		percent = DEFAULT_REVENUE_SHARE_PERCENT

	teacher_name = None
	if teacher and _doctype_available("Teacher"):
		teacher_fields = ["teacher_name"] if _has_field("Teacher", "teacher_name") else ["name"]
		teacher_row = frappe.db.get_value("Teacher", teacher, teacher_fields, as_dict=True)
		if teacher_row:
			teacher_name = teacher_row.get("teacher_name") or teacher

	return {
		"revenue_share_enabled": enabled,
		"revenue_share_source": "Course Session" if override != "Inherit from Weekly Timeslot" else "Weekly Timeslot",
		"revenue_share_override": override,
		"revenue_share_teacher": teacher if enabled else None,
		"revenue_share_teacher_name": teacher_name if enabled else None,
		"revenue_share_percent": flt(percent) if enabled else 0,
	}


def estimate_session_revenue(attendance_rows, course_doc):
	course_doc = _rowdict(course_doc)
	term_session_fee = _term_session_fee(course_doc)
	pay_as_you_go_fee = flt(course_doc.get("pay_as_you_go_fee"))
	if not pay_as_you_go_fee:
		pay_as_you_go_fee = term_session_fee

	counts = {
		"full_term_count": 0,
		"trial_count": 0,
		"pay_as_you_go_count": 0,
		"makeup_count": 0,
		"cancelled_count": 0,
		"other_count": 0,
	}
	for row in attendance_rows or []:
		status = row.get("status")
		enrollment_type = row.get("enrollment_type")
		if status == "Cancelled":
			counts["cancelled_count"] += 1
			continue
		if status not in REVENUE_ATTENDANCE_STATUSES:
			counts["other_count"] += 1
			continue
		if enrollment_type == "Full-Term":
			counts["full_term_count"] += 1
		elif enrollment_type == "Trial":
			counts["trial_count"] += 1
		elif enrollment_type == "Pay-as-you-go":
			counts["pay_as_you_go_count"] += 1
		elif enrollment_type == "Makeup":
			counts["makeup_count"] += 1
		else:
			counts["other_count"] += 1

	estimated_gross_revenue = (
		counts["full_term_count"] * term_session_fee
		+ counts["trial_count"] * pay_as_you_go_fee
		+ counts["pay_as_you_go_count"] * pay_as_you_go_fee
	)
	return {
		**counts,
		"estimated_gross_revenue": round(estimated_gross_revenue, 2),
		"revenue_basis": "estimated_from_attendance_and_course_pricing",
		"term_session_fee": term_session_fee,
		"pay_as_you_go_fee": pay_as_you_go_fee,
	}


def _get_sessions(from_date=None, to_date=None, campus=None, course=None, limit=200):
	filters = {}
	if from_date and to_date:
		filters["session_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["session_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["session_date"] = ["<=", getdate(to_date)]

	timeslot_ids = _get_matching_timeslot_ids(campus=campus, course=course)
	if timeslot_ids is not None:
		if not timeslot_ids:
			return []
		filters["weekly_timeslot"] = ["in", timeslot_ids]

	fields = _safe_fields(
		"Course Sessions",
		[
			"name",
			"weekly_timeslot",
			"session_date",
			"status",
			"revenue_share_override",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	return [
		_rowdict(row)
		for row in frappe.get_all(
			"Course Sessions",
			filters=filters,
			fields=fields,
			order_by="session_date asc, name asc",
			limit=_limit(limit),
		)
	]


def _get_matching_timeslot_ids(campus=None, course=None):
	if not _doctype_available("Weekly Timeslot"):
		return []
	filters = {}
	if campus and _has_field("Weekly Timeslot", "campus"):
		filters["campus"] = campus
	if course and _has_field("Weekly Timeslot", "course"):
		filters["course"] = course
	if not filters:
		return None
	return [row.name for row in frappe.get_all("Weekly Timeslot", filters=filters, fields=["name"])]


def _get_timeslot(timeslot_id):
	if not timeslot_id or not _doctype_available("Weekly Timeslot"):
		return {}
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
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	row = frappe.db.get_value("Weekly Timeslot", timeslot_id, fields, as_dict=True)
	return _rowdict(row) if row else {}


def _get_timeslot_map(timeslot_ids):
	timeslot_ids = sorted({row for row in timeslot_ids if row})
	if not timeslot_ids or not _doctype_available("Weekly Timeslot"):
		return {}
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
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	return {
		row.name: _rowdict(row)
		for row in frappe.get_all("Weekly Timeslot", filters={"name": ["in", timeslot_ids]}, fields=fields)
	}


def _get_course_map(course_ids):
	course_ids = sorted({row for row in course_ids if row})
	if not course_ids or not _doctype_available("Course"):
		return {}
	fields = _safe_fields("Course", ["name", "full_term_fee", "total_session_per_term", "term_session_fee", "pay_as_you_go_fee"])
	return {row.name: _rowdict(row) for row in frappe.get_all("Course", filters={"name": ["in", course_ids]}, fields=fields)}


def _get_attendance_map(course_session_ids):
	course_session_ids = sorted({row for row in course_session_ids if row})
	if not course_session_ids or not _doctype_available("Class Attendance Entry"):
		return {}
	fields = _safe_fields("Class Attendance Entry", ["name", "course_session", "enrollment_type", "status"])
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters={"course_session": ["in", course_session_ids]},
		fields=fields,
		limit=len(course_session_ids) * 200,
	)
	result = {}
	for row in rows:
		result.setdefault(row.course_session, []).append(_rowdict(row))
	return result


def _term_session_fee(course_doc):
	term_session_fee = flt(course_doc.get("term_session_fee"))
	if term_session_fee:
		return term_session_fee
	full_term_fee = flt(course_doc.get("full_term_fee"))
	total_sessions = flt(course_doc.get("total_session_per_term"))
	return round(full_term_fee / total_sessions, 2) if full_term_fee and total_sessions else 0


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


def _doctype_available(doctype):
	try:
		return bool(frappe.db.exists("DocType", doctype)) and bool(frappe.db.table_exists(doctype))
	except Exception:
		return False


def _rowdict(row):
	if not row:
		return {}
	return dict(row) if isinstance(row, dict) else row.as_dict()


def _limit(value):
	value = cint(value or 200)
	if value <= 0:
		value = 200
	return min(value, 500)
