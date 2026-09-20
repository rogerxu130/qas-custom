from __future__ import annotations

import csv
import io
import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import get_datetime_in_timezone, getdate

from qas_custom.modules.common import has_field


ADMIN_ROLES = {"School Admin", "System Manager"}
BRISBANE_TIMEZONE = "Australia/Brisbane"
SCOPE_TYPES = {"term", "weekly_timeslot", "workshop"}
NON_ATTENDING_STATUSES = {"Cancelled", "Leave"}
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
PARTICIPATION_ORDER = ("Full-Term", "Trial", "Makeup", "Pay-as-you-go", "Workshop")


def get_school_admin_parent_contact_export_options_data(scope_type=None, term=None, query=None, limit=200):
	_require_school_admin()
	scope_type = _validate_scope_type(scope_type)
	query = str(query or "").strip().casefold()
	limit = min(max(int(limit or 200), 1), 500)
	if scope_type == "term":
		return {"items": []}
	if scope_type == "weekly_timeslot":
		_validate_term(term)
		rows = frappe.get_all(
			"Weekly Timeslot",
			filters={"term": term},
			fields=_safe_fields("Weekly Timeslot", ["name", "term", "course", "campus", "teacher", "day_of_week", "start_time", "end_time", "status"]),
			order_by="course asc, campus asc, day_of_week asc, start_time asc",
			limit=0,
		)
		items = []
		for row in rows:
			label = " · ".join(str(row.get(field) or "").strip() for field in ("course", "campus", "day_of_week", "start_time", "teacher") if row.get(field))
			if query and query not in f"{row.get('name', '')} {label}".casefold():
				continue
			items.append({**dict(row), "label": label or row.get("name")})
		return {"items": items[:limit]}

	rows = frappe.get_all(
		"Workshop Offering",
		fields=_safe_fields("Workshop Offering", ["name", "title", "campus", "workshop_category", "status"]),
		order_by="modified desc",
		limit=0,
	)
	sessions = frappe.get_all(
		"Workshop Session",
		filters={"workshop_offering": ["in", [row.get("name") for row in rows] or [""]], "status": ["!=", "Cancelled"]},
		fields=["workshop_offering", "session_date"],
		order_by="session_date asc",
		limit=0,
	)
	dates = defaultdict(list)
	for session in sessions:
		if session.get("session_date"):
			dates[session.get("workshop_offering")].append(str(session.get("session_date")))
	items = []
	for row in rows:
		row_dates = dates.get(row.get("name"), [])
		date_label = row_dates[0] if len(row_dates) == 1 else (f"{row_dates[0]} – {row_dates[-1]}" if row_dates else "")
		label = " · ".join(value for value in [row.get("title") or row.get("name"), row.get("campus") or "", date_label] if value)
		if query and query not in f"{row.get('name', '')} {label}".casefold():
			continue
		items.append({**dict(row), "start_date": row_dates[0] if row_dates else "", "end_date": row_dates[-1] if row_dates else "", "label": label})
	return {"items": items[:limit]}


def get_school_admin_parent_contact_export_summary_data(scope_type=None, scope_name=None, term=None):
	_require_school_admin()
	rows, _label = _resolve_export(scope_type, scope_name, term)
	return {
		"participant_count": len(rows),
		"eligible_parent_count": len(rows),
		"missing_email_count": sum(1 for row in rows if not row.get("email")),
		"missing_sms_count": sum(1 for row in rows if not row.get("sms_number")),
	}


def export_school_admin_parent_contacts_data(scope_type=None, scope_name=None, term=None):
	_require_school_admin()
	rows, label = _resolve_export(scope_type, scope_name, term)
	if not rows:
		frappe.throw(_("This selection has no parent contacts to export."))
	frappe.local.response.filename = _export_filename(label)
	frappe.local.response.filecontent = _build_csv(rows)
	frappe.local.response.content_type = "text/csv; charset=utf-8"
	frappe.local.response.display_content_as = "attachment"
	frappe.local.response.type = "download"
	return None


def _resolve_export(scope_type, scope_name, term=None):
	scope_type = _validate_scope_type(scope_type)
	scope_name = str(scope_name or "").strip()
	if not scope_name:
		frappe.throw(_("Export selection is required."))
	if scope_type == "term":
		term_label = _validate_term(scope_name)
		return _term_rows(scope_name, term_label), term_label
	if scope_type == "weekly_timeslot":
		_validate_term(term)
		return _weekly_timeslot_rows(scope_name, term)
	return _workshop_rows(scope_name)


def _term_rows(term, term_label):
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"term": term, "status": ["in", ["Planned", "Active"]]},
		fields=_safe_fields("Enrollment", ["student", "parent", "course", "campus"]),
		limit=0,
	)
	students = _student_map([row.get("student") for row in enrollments])
	grouped = defaultdict(lambda: {"students": [], "campuses": []})
	for enrollment in enrollments:
		student = students.get(enrollment.get("student"), {})
		parent = enrollment.get("parent") or student.get("parent")
		if not parent:
			continue
		_add_unique(grouped[parent]["students"], student.get("label") or enrollment.get("student"))
		_add_unique(grouped[parent]["campuses"], enrollment.get("campus"))
	rows = []
	for parent, values in grouped.items():
		rows.append(_row(
			participant=", ".join(values["students"]), parent=parent, participation_types=["Full-Term"],
			source_label=term_label, campus=", ".join(values["campuses"]),
		))
	return _sorted_rows(rows)


def _weekly_timeslot_rows(weekly_timeslot, term):
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		weekly_timeslot,
		_safe_fields("Weekly Timeslot", ["name", "term", "course", "campus", "day_of_week", "start_time"]),
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly Timeslot was not found."))
	if timeslot.get("term") != term:
		frappe.throw(_("The selected class does not belong to this Term."))
	label = _timeslot_label(timeslot)
	participants = {}
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"weekly_timeslot": weekly_timeslot, "status": ["in", ["Planned", "Active"]]},
		fields=_safe_fields("Enrollment", ["student", "parent"]),
		limit=0,
	)
	student_ids = [row.get("student") for row in enrollments]

	term_end = frappe.db.get_value("Term", term, "end_date")
	if not term_end:
		frappe.throw(_("The selected Term has no end date."))
	today = get_datetime_in_timezone(BRISBANE_TIMEZONE).date()
	sessions = []
	if getdate(term_end) >= today:
		sessions = frappe.get_all(
			"Course Sessions",
			filters={
				"weekly_timeslot": weekly_timeslot,
				"session_date": ["between", [today, getdate(term_end)]],
				"status": ["!=", "Cancelled"],
			},
			fields=["name"],
			limit=0,
		)
	attendance = frappe.get_all(
		"Class Attendance Entry",
		filters={
			"course_session": ["in", [row.get("name") for row in sessions] or [""]],
			"status": ["not in", sorted(NON_ATTENDING_STATUSES)],
			"enrollment_type": ["in", ["Trial", "Makeup", "Pay-as-you-go"]],
		},
		fields=["student", "enrollment_type"],
		limit=0,
	)
	student_ids.extend(row.get("student") for row in attendance)
	students = _student_map(student_ids)
	for enrollment in enrollments:
		student_id = enrollment.get("student")
		student = students.get(student_id, {})
		key = (enrollment.get("parent") or student.get("parent") or "", student_id or "")
		participants.setdefault(key, {"student": student, "parent": key[0], "types": []})
		_add_unique(participants[key]["types"], "Full-Term")
	for entry in attendance:
		student_id = entry.get("student")
		student = students.get(student_id, {})
		key = (student.get("parent") or "", student_id or "")
		participants.setdefault(key, {"student": student, "parent": key[0], "types": []})
		_add_unique(participants[key]["types"], entry.get("enrollment_type"))
	rows = [
		_row(
			participant=value["student"].get("label") or key[1], parent=value["parent"],
			participation_types=value["types"], source_label=label, campus=timeslot.get("campus") or "",
		)
		for key, value in participants.items()
	]
	return _sorted_rows(rows), label


def _workshop_rows(workshop):
	offering = frappe.db.get_value(
		"Workshop Offering", workshop,
		_safe_fields("Workshop Offering", ["name", "title", "campus"]), as_dict=True,
	)
	if not offering:
		frappe.throw(_("Workshop Offering was not found."))
	label = offering.get("title") or offering.get("name")
	enrollments = frappe.get_all(
		"Workshop Enrollment",
		filters={"workshop_offering": workshop, "status": ["in", ["Planned", "Active", "Completed"]]},
		fields=_safe_fields("Workshop Enrollment", ["name", "student", "parent", "adult_participant_name", "adult_participant_parent"]),
		limit=0,
	)
	students = _student_map([row.get("student") for row in enrollments])
	rows = []
	seen_students = set()
	for enrollment in enrollments:
		student_id = enrollment.get("student")
		student = students.get(student_id, {})
		parent = enrollment.get("adult_participant_parent") or enrollment.get("parent") or student.get("parent")
		if enrollment.get("adult_participant_name"):
			participant = enrollment.get("adult_participant_name")
		elif student_id:
			key = (parent or "", student_id)
			if key in seen_students:
				continue
			seen_students.add(key)
			participant = student.get("label") or student_id
		else:
			participant = enrollment.get("adult_participant_name") or enrollment.get("name")
		rows.append(_row(
			participant=participant, parent=parent, participation_types=["Workshop"],
			source_label=label, campus=offering.get("campus") or "",
		))
	return _sorted_rows(rows), label


def _row(*, participant, parent, participation_types, source_label, campus):
	contact = _parent_contact(parent)
	return {
		"participant_name": participant or "",
		"parent_name": contact.get("parent_name") or parent or "",
		"email": contact.get("email") or "",
		"sms_number": contact.get("sms_number") or "",
		"participation_type": ", ".join(_sort_participation_types(participation_types)),
		"source_label": source_label or "",
		"campus": campus or "",
	}


def _parent_contact(parent):
	if not parent:
		return {}
	row = frappe.db.get_value(
		"Parent", parent,
		_safe_fields("Parent", ["name", "parent_name", "email", "email_id", "contact_email", "linked_user", "mobile_number"]),
		as_dict=True,
	)
	if not row:
		return {}
	email = next((str(row.get(field) or "").strip() for field in ("email", "email_id", "contact_email", "linked_user") if row.get(field)), "")
	return {
		"parent_name": row.get("parent_name") or row.get("name"),
		"email": email,
		"sms_number": str(row.get("mobile_number") or "").strip(),
	}


def _student_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Student", filters={"name": ["in", names]},
		fields=_safe_fields("Student", ["name", "student_name", "first_name", "last_name", "guardian", "parent"]),
		limit=0,
	)
	return {
		row.get("name"): {
			"parent": row.get("guardian") or row.get("parent"),
			"label": row.get("student_name") or " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or row.get("name"),
		}
		for row in rows
	}


def _build_csv(rows):
	buffer = io.StringIO(newline="")
	buffer.write("\ufeff")
	writer = csv.writer(buffer, lineterminator="\r\n")
	writer.writerow(["Student / Participant Name", "Parent Name", "Email", "SMS Number", "Participation Type", "Class / Workshop", "Campus"])
	for row in _sorted_rows(rows):
		writer.writerow([_safe_csv_value(row.get(field)) for field in ("participant_name", "parent_name", "email", "sms_number", "participation_type", "source_label", "campus")])
	return buffer.getvalue().encode("utf-8")


def _export_filename(label):
	name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(label or "contacts"))
	name = re.sub(r"\s+", "_", name).strip(" ._")
	name = re.sub(r"_+", "_", name)
	return f"{name or 'contacts'}_parent_contacts.csv"


def _safe_csv_value(value):
	text = str(value or "")
	return f"'{text}" if text.lstrip().startswith(CSV_FORMULA_PREFIXES) else text


def _sorted_rows(rows):
	return sorted(rows, key=lambda row: (str(row.get("participant_name") or "").casefold(), str(row.get("parent_name") or "").casefold()))


def _sort_participation_types(values):
	unique = {value for value in values if value}
	return sorted(unique, key=lambda value: (PARTICIPATION_ORDER.index(value) if value in PARTICIPATION_ORDER else len(PARTICIPATION_ORDER), value))


def _timeslot_label(row):
	return " · ".join(str(row.get(field) or "").strip() for field in ("course", "campus", "day_of_week", "start_time") if row.get(field)) or row.get("name")


def _add_unique(values, value):
	if value and value not in values:
		values.append(value)


def _validate_scope_type(scope_type):
	scope_type = str(scope_type or "").strip()
	if scope_type not in SCOPE_TYPES:
		frappe.throw(_("Export scope must be Term, Specific Class, or Workshop."))
	return scope_type


def _validate_term(term):
	term = str(term or "").strip()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	return frappe.db.get_value("Term", term, "term_name") or term


def _safe_fields(doctype, requested):
	return [field for field in requested if field == "name" or has_field(doctype, field)]


def _require_school_admin():
	if frappe.session.user == "Guest" or not set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can export parent contacts."), frappe.PermissionError)
