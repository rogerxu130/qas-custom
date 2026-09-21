from __future__ import annotations

import hashlib
import json
import re

import frappe
from frappe import _


SUPPORTED_TERM = "Term 4 2026"
WTS_PATTERN = re.compile(r"^WTS-(\d{4})-(\d{5})$")
SESSION_PATTERN = re.compile(r"^CS-(\d{4})-(\d{5})$")
DUPLICATE_FIELDS = ("course", "class_language", "campus", "classroom", "day_of_week", "start_time", "end_time")


def _require_access():
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user != "Administrator" and not roles.intersection({"System Manager", "School Admin"}):
		frappe.throw(_("Only School Admin or System Manager can run this migration."), frappe.PermissionError)


def _assert_term(term):
	if term != SUPPORTED_TERM:
		frappe.throw(_("This migration is restricted to {0}.").format(SUPPORTED_TERM))


def _rows(term):
	slots = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": term},
		fields=["name", "modified", "status", *DUPLICATE_FIELDS],
		order_by="name asc",
		limit_page_length=0,
	)
	slot_names = [row.name for row in slots]
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": ["in", slot_names]},
		fields=["name", "weekly_timeslot", "session_date", "status", "modified"],
		order_by="name asc",
		limit_page_length=0,
	) if slot_names else []
	return slots, sessions


def _duplicate_groups(slots):
	groups = {}
	for row in slots:
		key = tuple(str(row.get(field) or "") for field in DUPLICATE_FIELDS)
		groups.setdefault(key, []).append(row)
	return [
		{"records": [row.name for row in rows], "statuses": [row.status for row in rows]}
		for rows in groups.values() if len(rows) > 1
	]


def _proposed_names(rows, pattern, prefix):
	year = "2026"
	used = [int(match.group(2)) for row in rows if (match := pattern.match(row.name))]
	number = max(used or [0])
	result = {}
	for row in rows:
		if pattern.match(row.name):
			continue
		number += 1
		result[row.name] = f"{prefix}-{year}-{number:05d}"
	return result


def _state_payload(term, slots, sessions):
	return {
		"term": term,
		"weekly_timeslots": [(row.name, str(row.modified or "")) for row in slots],
		"course_sessions": [(row.name, row.weekly_timeslot, str(row.modified or "")) for row in sessions],
	}


def _token(payload):
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def preview(term=SUPPORTED_TERM):
	_require_access()
	_assert_term(term)
	slots, sessions = _rows(term)
	duplicates = _duplicate_groups(slots)
	state = _state_payload(term, slots, sessions)
	return {
		"term": term,
		"weekly_timeslot_count": len(slots),
		"course_session_count": len(sessions),
		"weekly_timeslot_mapping": _proposed_names(slots, WTS_PATTERN, "WTS"),
		"course_session_mapping": _proposed_names(sessions, SESSION_PATTERN, "CS"),
		"duplicate_groups": duplicates,
		"blocking_errors": [
			"Resolve duplicate Weekly Timeslots before migration: {0}.".format(", ".join(group["records"]))
			for group in duplicates
		],
		"confirmation_token": _token(state),
	}


def _advance_series(prefix, number):
	frappe.db.sql(
		"""INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, %s)
		ON DUPLICATE KEY UPDATE `current` = GREATEST(`current`, VALUES(`current`))""",
		(prefix, number),
	)


def execute(term=SUPPORTED_TERM, confirmation_token=None):
	_require_access()
	_assert_term(term)
	report = preview(term)
	if report["blocking_errors"]:
		frappe.throw("\n".join(report["blocking_errors"]))
	if not confirmation_token or confirmation_token != report["confirmation_token"]:
		frappe.throw(_("Migration state changed or confirmation token is missing. Run preview again."))

	result = {"term": term, "weekly_timeslots": [], "course_sessions": []}
	for old_name, new_name in report["weekly_timeslot_mapping"].items():
		frappe.rename_doc("Weekly Timeslot", old_name, new_name, force=False, merge=False, ignore_permissions=True)
		result["weekly_timeslots"].append({"old": old_name, "new": new_name})
	for old_name, new_name in report["course_session_mapping"].items():
		frappe.rename_doc("Course Sessions", old_name, new_name, force=False, merge=False, ignore_permissions=True)
		result["course_sessions"].append({"old": old_name, "new": new_name})

	if report["weekly_timeslot_mapping"]:
		_advance_series("WTS-2026-", max(int(name.rsplit("-", 1)[1]) for name in report["weekly_timeslot_mapping"].values()))
	if report["course_session_mapping"]:
		_advance_series("CS-2026-", max(int(name.rsplit("-", 1)[1]) for name in report["course_session_mapping"].values()))
	frappe.db.commit()
	result["verification"] = verify(term)
	return result


def verify(term=SUPPORTED_TERM):
	_require_access()
	_assert_term(term)
	slots, sessions = _rows(term)
	long_slots = [row.name for row in slots if not WTS_PATTERN.match(row.name)]
	long_sessions = [row.name for row in sessions if not SESSION_PATTERN.match(row.name)]
	invalid_session_links = [row.name for row in sessions if not frappe.db.exists("Weekly Timeslot", row.weekly_timeslot)]
	return {
		"term": term,
		"weekly_timeslot_count": len(slots),
		"course_session_count": len(sessions),
		"long_weekly_timeslots": long_slots,
		"long_course_sessions": long_sessions,
		"invalid_course_session_links": invalid_session_links,
		"ok": not long_slots and not long_sessions and not invalid_session_links and not _duplicate_groups(slots),
	}
