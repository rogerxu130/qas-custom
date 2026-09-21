from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import getdate, today

from qas_custom.services.school_admin import _add_comment, _require_school_admin


def _state_token(payload):
	state = {
		"weekly_timeslot": payload["weekly_timeslot"],
		"old_course": payload["old_course"],
		"new_course": payload["new_course"],
		"enrollments": [row["name"] for row in payload["enrollments"]],
		"sessions": [row["name"] for row in payload["sessions"]],
		"draft_invoices": payload["draft_invoices"],
		"trial_inquiries": payload["trial_inquiries"],
	}
	return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _preview(weekly_timeslot, new_course):
	if not weekly_timeslot or not new_course:
		frappe.throw(_("Weekly Timeslot and new course are required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	if not frappe.db.exists("Course", new_course):
		frappe.throw(_("Course {0} does not exist.").format(new_course))
	term = frappe.db.get_value("Term", doc.term, ["status", "start_date"], as_dict=True) or frappe._dict()
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": doc.name},
		fields=["name", "session_date", "status"],
		order_by="session_date asc",
		limit_page_length=0,
	)
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"weekly_timeslot": doc.name, "enrollment_type": "Full-Term", "status": ["in", ["Planned", "Active"]]},
		fields=["name", "status", "course", "invoice", "invoice_status", "invoice_amount"],
		order_by="name asc",
		limit_page_length=0,
	)
	session_names = [row.name for row in sessions]
	trial_filters = {"course_session": ["in", session_names], "inquiry_type": "Trial Lesson"} if session_names else None
	trial_inquiries = frappe.get_all("Inquiry", filters=trial_filters, pluck="name", limit_page_length=0) if trial_filters else []
	draft_invoices = sorted({row.invoice for row in enrollments if row.get("invoice") and row.get("invoice_status") == "Draft"})
	blocking_errors = []
	if term.get("status") != "Upcoming":
		blocking_errors.append(_("The class course can only be changed while the term is Upcoming."))
	if any(row.get("status") == "Completed" or getdate(row.get("session_date")) < getdate(today()) for row in sessions):
		blocking_errors.append(_("A linked course session has already started or completed."))
	if session_names and frappe.db.exists(
		"Class Attendance Entry",
		{"course_session": ["in", session_names], "status": ["not in", ["Scheduled", "Not Marked", "Cancelled"]]},
	):
		blocking_errors.append(_("Attendance has already been marked for a linked session."))
	old_duration = frappe.db.get_value("Course", doc.course, "duration_mins") or 0
	new_duration = frappe.db.get_value("Course", new_course, "duration_mins") or 0
	payload = {
		"weekly_timeslot": doc.name,
		"term": doc.term,
		"old_course": doc.course,
		"new_course": new_course,
		"enrollments": [dict(row) for row in enrollments],
		"planned_enrollment_count": sum(row.status == "Planned" for row in enrollments),
		"active_enrollment_count": sum(row.status == "Active" for row in enrollments),
		"sessions": [dict(row) for row in sessions],
		"future_session_count": len(sessions),
		"draft_invoices": draft_invoices,
		"trial_inquiries": sorted(trial_inquiries),
		"old_duration_mins": int(old_duration),
		"new_duration_mins": int(new_duration),
		"duration_changed": int(old_duration) != int(new_duration),
		"blocking_errors": blocking_errors,
		"warnings": [
			*([_("Review the listed Draft invoices; no invoice will be changed automatically.")] if draft_invoices else []),
			*([_("Review the listed trial inquiries; their submitted course choice will not be changed automatically.")] if trial_inquiries else []),
		],
	}
	payload["confirmation_token"] = _state_token(payload)
	return payload


@frappe.whitelist()
def preview(weekly_timeslot=None, new_course=None):
	_require_school_admin()
	return _preview(weekly_timeslot, new_course)


@frappe.whitelist()
def execute(weekly_timeslot=None, new_course=None, confirmation_token=None):
	_require_school_admin()
	report = _preview(weekly_timeslot, new_course)
	if report["blocking_errors"]:
		frappe.throw("\n".join(report["blocking_errors"]))
	if not confirmation_token or confirmation_token != report["confirmation_token"]:
		frappe.throw(_("Course-change data changed or confirmation is missing. Preview again."))
	if report["old_course"] == report["new_course"]:
		return {**report, "updated_enrollment_count": 0}

	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	doc.course = new_course
	doc.save(ignore_permissions=True)
	for row in report["enrollments"]:
		frappe.db.set_value("Enrollment", row["name"], "course", new_course, update_modified=True)
		_add_comment("Enrollment", row["name"], _("Course synchronized from {0} to {1} after reviewed Weekly Timeslot change.").format(report["old_course"], new_course))
	_add_comment(
		"Weekly Timeslot",
		doc.name,
		_("Reviewed pre-term course change from {0} to {1}; synchronized {2} enrollment(s). Draft invoices and trial inquiries were not modified.").format(
			report["old_course"], new_course, len(report["enrollments"])
		),
	)
	frappe.db.commit()
	return {**report, "updated_enrollment_count": len(report["enrollments"]), "completed": True}
