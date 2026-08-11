"""NDIS-friendly course capacity helpers.

An NDIS-friendly course is a weekly timeslot.  Its advertised capacity is
based on unique, open full-term enrolments (Planned or Active), rather than
one-off trials or makeup attendance on an individual date.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint

from qas_custom.services.maintenance import record_data_issue, resolve_data_issue


NDIS_FRIENDLY_CAPACITY = 4
NDIS_CAPACITY_ISSUE_PREFIX = "ndis-friendly-capacity"
OPEN_ENROLLMENT_STATUSES = ("Planned", "Active")


def get_ndis_friendly_capacity_statuses(weekly_timeslots):
	"""Return public-listing and permanent-roster capacity for each timeslot."""
	timeslot_ids = sorted({str(name) for name in weekly_timeslots if name})
	if not timeslot_ids:
		return {}
	if not _doctype_available("Weekly Timeslot"):
		return {}

	fields = ["name"]
	if _has_field("Weekly Timeslot", "ndis_friendly"):
		fields.append("ndis_friendly")
	if _has_field("Weekly Timeslot", "ndis_public_listing_enabled"):
		fields.append("ndis_public_listing_enabled")
	if _has_field("Weekly Timeslot", "ndis_capacity_alert_active"):
		fields.append("ndis_capacity_alert_active")
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", timeslot_ids]},
		fields=fields,
		limit_page_length=0,
	)
	settings = {row.get("name"): row for row in timeslots}
	student_ids_by_timeslot = _open_enrollment_students_by_timeslot(timeslot_ids)

	statuses = {}
	for name in timeslot_ids:
		row = settings.get(name) or {}
		friendly = bool(cint(row.get("ndis_friendly")))
		student_count = len(student_ids_by_timeslot.get(name, set()))
		statuses[name] = {
			"ndis_friendly": friendly,
			"ndis_public_listing_enabled": bool(cint(row.get("ndis_public_listing_enabled"))) if friendly else False,
			"ndis_capacity": NDIS_FRIENDLY_CAPACITY,
			"ndis_enrollment_count": student_count,
			"ndis_capacity_reached": friendly and student_count >= NDIS_FRIENDLY_CAPACITY,
			"ndis_capacity_exceeded": friendly and student_count > NDIS_FRIENDLY_CAPACITY,
			"ndis_capacity_alert_active": bool(cint(row.get("ndis_capacity_alert_active"))),
		}
	return statuses


def get_ndis_friendly_capacity_status(weekly_timeslot):
	return get_ndis_friendly_capacity_statuses([weekly_timeslot]).get(weekly_timeslot, _empty_status())


def refresh_ndis_friendly_capacity_alert(weekly_timeslot, notify=True):
	"""Alert once per continuous at-capacity period, respecting admin acknowledgement."""
	status = get_ndis_friendly_capacity_status(weekly_timeslot)
	issue_key = _issue_key(weekly_timeslot)
	if not status.get("ndis_capacity_reached"):
		alert_was_active = status.get("ndis_capacity_alert_active")
		if alert_was_active:
			_set_ndis_capacity_alert_active(weekly_timeslot, False)
		resolved = resolve_data_issue(issue_key)
		if resolved or alert_was_active:
			frappe.db.commit()
		return {**status, "issue": None, "issue_created": False, "issue_reopened": False, "issue_resolved": bool(resolved)}

	issue_status = _data_issue_status(issue_key)
	if status.get("ndis_capacity_alert_active") and issue_status in {"Resolved", "Ignored"}:
		return {
			**status,
			"issue": None,
			"issue_created": False,
			"issue_reopened": False,
			"issue_resolved": False,
			"issue_acknowledged": True,
		}

	result = record_data_issue(
		{
			"issue_key": issue_key,
			"issue_type": "NDIS Capacity",
			"severity": "Critical" if status.get("ndis_capacity_exceeded") else "Warning",
			"source_doctype": "Weekly Timeslot",
			"source_document": weekly_timeslot,
			"related_doctype": "Enrollment",
			"related_document": None,
			"student": None,
			"course_session": None,
			"description": _(
				"NDIS-friendly weekly timeslot {0} has {1} Planned or Active full-term student(s); its public capacity is {2}."
			).format(weekly_timeslot, status["ndis_enrollment_count"], status["ndis_capacity"]),
			"suggested_action": _(
				"Review the roster. If this class should stop accepting NDIS enquiries, turn off Show on public NDIS listing in School Admin. Then set this issue to Resolved or Ignored to suppress repeat alerts until the roster drops below capacity."
			),
		},
		notify=notify,
		notify_on_reopen=True,
	)
	_set_ndis_capacity_alert_active(weekly_timeslot, True)
	return {
		**status,
		"issue": result.get("issue"),
		"issue_created": bool(result.get("created")),
		"issue_reopened": bool(result.get("reopened")),
		"issue_resolved": False,
		"issue_acknowledged": False,
	}


def reconcile_ndis_friendly_capacity():
	"""Nightly safety net for imports or backend edits outside School Admin."""
	if not _doctype_available("Weekly Timeslot") or not _has_field("Weekly Timeslot", "ndis_friendly"):
		return {"skipped": True, "reason": "NDIS Friendly fields are not available."}
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"ndis_friendly": 1},
		pluck="name",
		limit_page_length=0,
	)
	results = [refresh_ndis_friendly_capacity_alert(name) for name in timeslots]
	return {
		"checked": len(timeslots),
		"at_capacity": len([row for row in results if row.get("ndis_capacity_reached")]),
		"new_issues": [row.get("issue") for row in results if row.get("issue_created") and row.get("issue")],
	}


def _open_enrollment_students_by_timeslot(weekly_timeslots):
	if not _doctype_available("Enrollment"):
		return {}
	if not _has_field("Enrollment", "weekly_timeslot") or not _has_field("Enrollment", "student"):
		return {}
	filters = {"weekly_timeslot": ["in", weekly_timeslots]}
	if _has_field("Enrollment", "status"):
		filters["status"] = ["in", list(OPEN_ENROLLMENT_STATUSES)]
	rows = frappe.get_all(
		"Enrollment",
		filters=filters,
		fields=["weekly_timeslot", "student"],
		limit_page_length=0,
	)
	students = defaultdict(set)
	for row in rows:
		if row.get("weekly_timeslot") and row.get("student"):
			students[row.get("weekly_timeslot")].add(row.get("student"))
	return students


def _empty_status():
	return {
		"ndis_friendly": False,
		"ndis_public_listing_enabled": False,
		"ndis_capacity": NDIS_FRIENDLY_CAPACITY,
		"ndis_enrollment_count": 0,
		"ndis_capacity_reached": False,
		"ndis_capacity_exceeded": False,
		"ndis_capacity_alert_active": False,
	}


def _issue_key(weekly_timeslot):
	return f"{NDIS_CAPACITY_ISSUE_PREFIX}:{weekly_timeslot}"


def _data_issue_status(issue_key):
	if not _doctype_available("QAS Data Issue"):
		return None
	return frappe.db.get_value("QAS Data Issue", {"issue_key": issue_key}, "status")


def _set_ndis_capacity_alert_active(weekly_timeslot, active):
	if not weekly_timeslot or not _has_field("Weekly Timeslot", "ndis_capacity_alert_active"):
		return False
	frappe.db.set_value(
		"Weekly Timeslot",
		weekly_timeslot,
		"ndis_capacity_alert_active",
		cint(active),
		update_modified=False,
	)
	return True


def _doctype_available(doctype):
	return bool(frappe.db.exists("DocType", doctype))


def _has_field(doctype, fieldname):
	return bool(frappe.get_meta(doctype).get_field(fieldname))
