from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import getdate, get_datetime, now_datetime, today

from qas_custom.services.class_record_labels import refresh_linked_course_session_labels
from qas_custom.services.school_admin import (
	_add_comment,
	_apply_weekly_timeslot_payload,
	_assert_active_teacher,
	_course_session_date_exists,
	_course_session_has_locked_attendance,
	_format_time_for_message,
	_future_weekday_dates,
	_get_payload,
	_require_school_admin,
	_time_to_minutes,
	_validate_weekly_timeslot_change,
)

EDITABLE_FIELDS = (
	"course", "class_language", "campus", "classroom", "teacher", "day_of_week",
	"start_time", "end_time", "status", "ndis_friendly",
)


def _normal_value(fieldname, value):
	if fieldname in {"start_time", "end_time"}:
		return _format_time_for_message(value)
	if fieldname == "ndis_friendly":
		return int(value or 0)
	return value or ""


def _session_has_started(session, start_time, current_time):
	if session.get("status") == "Completed":
		return True
	# Use the saved start time, never the proposed time, to protect today's history.
	start = _format_time_for_message(start_time) or "00:00"
	return get_datetime(f"{getdate(session.get('session_date'))} {start}") <= current_time


def _intended_payload(doc, new_course, payload=None):
	payload = _get_payload(payload) if payload is not None else {}
	unknown = sorted(set(payload) - set(EDITABLE_FIELDS) - {"term"})
	if unknown:
		frappe.throw(_("Unsupported Weekly Timeslot fields: {0}").format(", ".join(unknown)))
	if payload.get("term") and payload.get("term") != doc.term:
		frappe.throw(_("The term cannot be changed during a reviewed course change."))
	if payload.get("course") and payload.get("course") != new_course:
		frappe.throw(_("The reviewed course does not match the submitted Weekly Timeslot course."))
	intended = {field: _normal_value(field, doc.get(field)) for field in EDITABLE_FIELDS}
	for field in EDITABLE_FIELDS:
		if field in payload:
			intended[field] = _normal_value(field, payload.get(field))
	intended["course"] = new_course
	return intended


def _state_token(report):
	state = {
		"teacher_change_policy": report["teacher_change_policy"],
		"weekly_timeslot": report["weekly_timeslot"],
		"changes": report["changes"],
		"intended_payload": report["intended_payload"],
		"enrollments": report["enrollments"],
		"sessions": report["sessions"],
		"draft_invoices": report["draft_invoices"],
		"trial_inquiries": report["trial_inquiries"],
		"effective_date": report["effective_date"],
		"session_date_mapping": report["session_date_mapping"],
	}
	return hashlib.sha256(json.dumps(state, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _teacher_conflict(doc, intended):
	if not intended["teacher"] or intended["status"] != "Active":
		return None
	start = _time_to_minutes(intended["start_time"], _("Start time is invalid."))
	end = _time_to_minutes(intended["end_time"], _("End time is invalid."))
	for row in frappe.get_all(
		"Weekly Timeslot",
		filters={
			"term": doc.term, "teacher": intended["teacher"],
			"day_of_week": intended["day_of_week"], "status": "Active",
		},
		fields=["name", "course", "start_time", "end_time"], limit_page_length=0,
	):
		if row.name == doc.name:
			continue
		row_start = _time_to_minutes(row.start_time, _("Start time is invalid."))
		row_end = _time_to_minutes(row.end_time, _("End time is invalid."))
		if start < row_end and row_start < end:
			return _("Teacher {0} is already assigned to {1} at {2}-{3}.").format(
				intended["teacher"], row.get("course") or row.name,
				_format_time_for_message(row.start_time), _format_time_for_message(row.end_time),
			)
	return None


def _preview(weekly_timeslot, new_course, payload=None, effective_date=None):
	if not weekly_timeslot or not new_course:
		frappe.throw(_("Weekly Timeslot and new course are required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	if not frappe.db.exists("Course", new_course):
		frappe.throw(_("Course {0} does not exist.").format(new_course))
	term = frappe.db.get_value("Term", doc.term, ["status", "start_date", "end_date"], as_dict=True) or frappe._dict()
	intended = _intended_payload(doc, new_course, payload)
	before = {field: _normal_value(field, doc.get(field)) for field in EDITABLE_FIELDS}
	changes = {field: {"before": before[field], "after": intended[field]} for field in EDITABLE_FIELDS if before[field] != intended[field]}

	sessions = frappe.get_all(
		"Course Sessions", filters={"weekly_timeslot": doc.name},
		fields=["name", "session_date", "status", "teacher_override"],
		order_by="session_date asc", limit_page_length=0,
	)
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"weekly_timeslot": doc.name, "enrollment_type": "Full-Term", "status": ["in", ["Planned", "Active"]]},
		fields=["name", "status", "course", "invoice", "invoice_status", "invoice_amount"],
		order_by="name asc", limit_page_length=0,
	)
	session_names = [row.name for row in sessions]
	trial_filters = {"course_session": ["in", session_names], "inquiry_type": "Trial Lesson"} if session_names else None
	trial_inquiries = frappe.get_all("Inquiry", filters=trial_filters, pluck="name", limit_page_length=0) if trial_filters else []
	draft_invoices = sorted({row.invoice for row in enrollments if row.get("invoice") and row.get("invoice_status") == "Draft"})
	blocking_errors = []
	if "course" in changes and term.get("status") != "Upcoming":
		blocking_errors.append(_("The class course can only be changed while the term is Upcoming."))
	current_time = now_datetime()
	if any(_session_has_started(row, doc.get("start_time"), current_time) for row in sessions):
		blocking_errors.append(_("A linked course session has already started or completed. Use Change weekly teacher from the Classes workspace to change the teacher."))
	if session_names and frappe.db.exists(
		"Class Attendance Entry",
		{"course_session": ["in", session_names], "status": ["not in", ["Scheduled", "Not Marked", "To be started", "Cancelled"]]},
	):
		blocking_errors.append(_("Attendance has already been marked for a linked session. Use Change weekly teacher from the Classes workspace to change the teacher."))
	if intended["teacher"]:
		_assert_active_teacher(intended["teacher"])

	schedule_changed = bool({"day_of_week", "start_time", "end_time"}.intersection(changes))
	teacher_changed = "teacher" in changes
	effective = getdate(effective_date) if effective_date else None
	# All sessions must inherit this change, including sessions before a supplied term start.
	if teacher_changed:
		effective = getdate(today())
	if sessions and (schedule_changed or teacher_changed) and not effective:
		blocking_errors.append(_("An effective date is required for combined schedule or teacher changes."))
	if effective and effective < getdate(today()):
		blocking_errors.append(_("Effective date cannot be before today."))

	validation_payload = {key: value for key, value in intended.items() if key != "course"}
	if schedule_changed and sessions:
		validation_payload.update({"apply_future_sessions": 1, "effective_date": str(effective) if effective else ""})
	_validate_weekly_timeslot_change(doc, validation_payload)
	_apply_weekly_timeslot_payload(doc, intended)
	if {"teacher", "day_of_week", "start_time", "end_time", "status"}.intersection(changes):
		if conflict := _teacher_conflict(doc, intended):
			blocking_errors.append(conflict)

	date_mapping = []
	if "day_of_week" in changes and sessions and effective:
		affected = [row for row in sessions if getdate(row.session_date) >= effective]
		target_dates = _future_weekday_dates(effective, intended["day_of_week"], len(affected))
		if term.get("end_date") and any(date > getdate(term.end_date) for date in target_dates):
			blocking_errors.append(_("The weekday change would move a session beyond the term end date."))
		for session, target_date in zip(affected, target_dates):
			if getdate(session.session_date) == target_date:
				continue
			if _course_session_date_exists(doc.name, target_date, exclude=session.name):
				blocking_errors.append(_("A target session date already exists: {0}").format(target_date))
			if _course_session_has_locked_attendance(session.name):
				blocking_errors.append(_("Attendance locks session {0}; its date cannot be changed.").format(session.name))
			date_mapping.append({"session": session.name, "before": str(session.session_date), "after": str(target_date)})

	old_duration = frappe.db.get_value("Course", before["course"], "duration_mins") or 0
	new_duration = frappe.db.get_value("Course", new_course, "duration_mins") or 0
	report = {
		"teacher_change_policy": "all_unstarted_sessions",
		"weekly_timeslot": doc.name, "term": doc.term,
		"old_course": before["course"], "new_course": new_course,
		"intended_payload": intended, "changes": changes,
		"effective_date": str(effective) if effective else "",
		"session_date_mapping": date_mapping,
		"enrollments": [dict(row) for row in enrollments],
		"planned_enrollment_count": sum(row.status == "Planned" for row in enrollments),
		"active_enrollment_count": sum(row.status == "Active" for row in enrollments),
		"sessions": [dict(row) for row in sessions], "future_session_count": len(sessions),
		"draft_invoices": draft_invoices, "trial_inquiries": sorted(trial_inquiries),
		"old_duration_mins": int(old_duration), "new_duration_mins": int(new_duration),
		"duration_changed": int(old_duration) != int(new_duration),
		"future_override_count": sum(bool(row.get("teacher_override")) and (not effective or getdate(row.session_date) >= effective) for row in sessions),
		"blocking_errors": blocking_errors,
		"warnings": [
			*([_("Review the listed Draft invoices; no invoice will be changed automatically.")] if draft_invoices else []),
			*([_("Review the listed trial inquiries; their submitted course choice will not be changed automatically.")] if trial_inquiries else []),
		],
	}
	report["confirmation_token"] = _state_token(report)
	return report


@frappe.whitelist()
def preview(weekly_timeslot=None, new_course=None, payload=None, effective_date=None):
	_require_school_admin()
	return _preview(weekly_timeslot, new_course, payload, effective_date)


@frappe.whitelist()
def execute(weekly_timeslot=None, new_course=None, confirmation_token=None, payload=None, effective_date=None):
	_require_school_admin()
	report = _preview(weekly_timeslot, new_course, payload, effective_date)
	if report["blocking_errors"]:
		frappe.throw("\n".join(report["blocking_errors"]))
	if not confirmation_token or confirmation_token != report["confirmation_token"]:
		frappe.throw(_("Class data changed or confirmation is missing. Preview again."))

	try:
		doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
		intended = report["intended_payload"]
		if "teacher" in report["changes"]:
			for session in report["sessions"]:
				if session.get("teacher_override"):
					frappe.db.set_value("Course Sessions", session["name"], "teacher_override", "", update_modified=True)
		_apply_weekly_timeslot_payload(doc, intended)
		doc.save(ignore_permissions=True)
		for mapping in report["session_date_mapping"]:
			frappe.db.set_value("Course Sessions", mapping["session"], "session_date", mapping["after"], update_modified=True)
		changed_enrollments = report["enrollments"] if "course" in report["changes"] else []
		for row in changed_enrollments:
			frappe.db.set_value("Enrollment", row["name"], "course", new_course, update_modified=True)
			_add_comment("Enrollment", row["name"], _("Course synchronized from {0} to {1} after reviewed class change.").format(report["old_course"], new_course))
		refresh_linked_course_session_labels(doc)
		_add_comment(
			"Weekly Timeslot", doc.name,
			_("Reviewed combined class change: {0}. Synchronized {1} enrollment(s); invoices, trial inquiries and attendance were not modified.").format(
				", ".join(sorted(report["changes"])), len(changed_enrollments)
			),
		)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	return {**report, "updated_enrollment_count": len(changed_enrollments), "completed": True}
