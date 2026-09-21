from __future__ import annotations

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.model.rename_doc import rename_doc


SUPPORTED_TERM = "Term 4 2026"
WTS_PATTERN = re.compile(r"^WTS-(\d{4})-(\d{5})$")
SESSION_PATTERN = re.compile(r"^CS-(\d{4})-(\d{5})$")
DUPLICATE_FIELDS = ("course", "class_language", "campus", "classroom", "day_of_week", "start_time", "end_time")


def _link_fields(target_doctype):
	fields = []
	for meta_doctype, owner_field in (("DocField", "parent"), ("Custom Field", "dt")):
		for row in frappe.get_all(
			meta_doctype,
			filters={"fieldtype": "Link", "options": target_doctype},
			fields=[owner_field, "fieldname"],
			limit_page_length=0,
		):
			item = (row[owner_field], row.fieldname)
			if item not in fields:
				fields.append(item)
	return sorted(fields)


def _reference_inventory(target_doctype, names):
	result = []
	for doctype, fieldname in _link_fields(target_doctype):
		for name in names:
			count = frappe.db.count(doctype, {fieldname: name})
			if count:
				result.append({"doctype": doctype, "fieldname": fieldname, "target": name, "count": count})
	return result


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


def _state_payload(term, slots, sessions, weekly_timeslot_references=None, course_session_references=None):
	return {
		"term": term,
		"weekly_timeslots": [(row.name, str(row.modified or "")) for row in slots],
		"course_sessions": [(row.name, row.weekly_timeslot, str(row.modified or "")) for row in sessions],
		"weekly_timeslot_references": weekly_timeslot_references or [],
		"course_session_references": course_session_references or [],
	}


def _token(payload):
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def preview(term=SUPPORTED_TERM):
	_require_access()
	_assert_term(term)
	slots, sessions = _rows(term)
	duplicates = _duplicate_groups(slots)
	weekly_timeslot_references = _reference_inventory("Weekly Timeslot", [row.name for row in slots])
	course_session_references = _reference_inventory("Course Sessions", [row.name for row in sessions])
	state = _state_payload(term, slots, sessions, weekly_timeslot_references, course_session_references)
	return {
		"term": term,
		"weekly_timeslot_count": len(slots),
		"course_session_count": len(sessions),
		"weekly_timeslot_mapping": _proposed_names(slots, WTS_PATTERN, "WTS"),
		"course_session_mapping": _proposed_names(sessions, SESSION_PATTERN, "CS"),
		"duplicate_groups": duplicates,
		"weekly_timeslot_references": weekly_timeslot_references,
		"course_session_references": course_session_references,
		"blocking_errors": [
			"Resolve duplicate Weekly Timeslots before migration: {0}.".format(", ".join(group["records"]))
			for group in duplicates
		],
		"confirmation_token": _token(state),
	}


def _duplicate_pair(canonical, duplicate):
	if not canonical or not duplicate or canonical == duplicate:
		frappe.throw(_("Two different Weekly Timeslots are required."))
	canonical_doc = frappe.get_doc("Weekly Timeslot", canonical)
	duplicate_doc = frappe.get_doc("Weekly Timeslot", duplicate)
	if canonical_doc.get("term") != SUPPORTED_TERM or duplicate_doc.get("term") != SUPPORTED_TERM:
		frappe.throw(_("Duplicate consolidation is restricted to {0}.").format(SUPPORTED_TERM))
	if any(str(canonical_doc.get(field) or "") != str(duplicate_doc.get(field) or "") for field in DUPLICATE_FIELDS):
		frappe.throw(_("The selected Weekly Timeslots are not exact business duplicates."))
	return canonical_doc, duplicate_doc


def _sessions_by_date(weekly_timeslot):
	rows = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": weekly_timeslot},
		fields=["name", "weekly_timeslot", "session_date", "status", "modified"],
		order_by="session_date asc, name asc",
		limit_page_length=0,
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(str(row.session_date), []).append(row)
	return rows, grouped


def preview_duplicate_consolidation(canonical=None, duplicate=None):
	_require_access()
	canonical_doc, duplicate_doc = _duplicate_pair(canonical, duplicate)
	canonical_sessions, canonical_dates = _sessions_by_date(canonical_doc.name)
	duplicate_sessions, duplicate_dates = _sessions_by_date(duplicate_doc.name)
	blocking_errors = []
	for date, rows in {**canonical_dates, **duplicate_dates}.items():
		if len(canonical_dates.get(date, [])) > 1 or len(duplicate_dates.get(date, [])) > 1:
			blocking_errors.append(_("Multiple Course Sessions exist on {0}; consolidation is ambiguous.").format(date))
	session_actions = []
	for date, rows in duplicate_dates.items():
		duplicate_session = rows[0]
		canonical_rows = canonical_dates.get(date, [])
		if canonical_rows:
			session_actions.append({"action": "merge", "date": date, "source": duplicate_session.name, "target": canonical_rows[0].name})
		else:
			session_actions.append({"action": "rebind", "date": date, "source": duplicate_session.name, "target_weekly_timeslot": canonical_doc.name})

	names = [canonical_doc.name, duplicate_doc.name]
	session_names = [row.name for row in canonical_sessions + duplicate_sessions]
	references = {
		"weekly_timeslots": _reference_inventory("Weekly Timeslot", names),
		"course_sessions": _reference_inventory("Course Sessions", session_names),
	}
	enrollments = frappe.get_all(
		"Enrollment",
		filters={"weekly_timeslot": ["in", names]},
		fields=["name", "weekly_timeslot", "start_course_session", "invoice", "invoice_status", "invoice_amount"],
		order_by="name asc",
		limit_page_length=0,
	)
	invoice_snapshot = [
		{"enrollment": row.name, "invoice": row.get("invoice"), "invoice_status": row.get("invoice_status"), "invoice_amount": row.get("invoice_amount")}
		for row in enrollments if row.get("invoice")
	]
	state = {
		"canonical": canonical_doc.name,
		"canonical_modified": str(canonical_doc.modified),
		"duplicate": duplicate_doc.name,
		"duplicate_modified": str(duplicate_doc.modified),
		"session_actions": session_actions,
		"references": references,
		"invoice_snapshot": invoice_snapshot,
	}
	return {
		**state,
		"canonical_session_count": len(canonical_sessions),
		"duplicate_session_count": len(duplicate_sessions),
		"enrollment_count": len(enrollments),
		"blocking_errors": blocking_errors,
		"confirmation_token": _token(state),
	}


def execute_duplicate_consolidation(canonical=None, duplicate=None, confirmation_token=None):
	_require_access()
	report = preview_duplicate_consolidation(canonical, duplicate)
	if report["blocking_errors"]:
		frappe.throw("\n".join(report["blocking_errors"]))
	if not confirmation_token or confirmation_token != report["confirmation_token"]:
		frappe.throw(_("Duplicate state changed or confirmation token is missing. Run the consolidation preview again."))

	for action in report["session_actions"]:
		if action["action"] == "merge":
			rename_doc(
				"Course Sessions", action["source"], action["target"],
				force=True, merge=True, ignore_permissions=True,
			)
		else:
			frappe.db.set_value(
				"Course Sessions", action["source"], "weekly_timeslot", canonical,
				update_modified=True,
			)

	rename_doc("Weekly Timeslot", duplicate, canonical, force=True, merge=True, ignore_permissions=True)
	remaining = _reference_inventory("Weekly Timeslot", [duplicate])
	if remaining or frappe.db.exists("Weekly Timeslot", duplicate):
		frappe.throw(_("Duplicate Weekly Timeslot still has references after consolidation; transaction aborted."))
	for snapshot in report["invoice_snapshot"]:
		current = frappe.db.get_value(
			"Enrollment", snapshot["enrollment"],
			["invoice", "invoice_status", "invoice_amount"], as_dict=True,
		) or frappe._dict()
		if any(current.get(field) != snapshot.get(field) for field in ("invoice", "invoice_status", "invoice_amount")):
			frappe.throw(_("Invoice fields changed unexpectedly for Enrollment {0}; transaction aborted.").format(snapshot["enrollment"]))
	frappe.db.commit()
	return {
		"canonical": canonical,
		"merged_duplicate": duplicate,
		"session_actions": report["session_actions"],
		"invoice_snapshot": report["invoice_snapshot"],
		"remaining_duplicate_references": remaining,
		"ok": True,
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
		rename_doc("Weekly Timeslot", old_name, new_name, force=False, merge=False, ignore_permissions=True)
		result["weekly_timeslots"].append({"old": old_name, "new": new_name})
	for old_name, new_name in report["course_session_mapping"].items():
		rename_doc("Course Sessions", old_name, new_name, force=False, merge=False, ignore_permissions=True)
		result["course_sessions"].append({"old": old_name, "new": new_name})

	remaining_old_references = [
		*_reference_inventory("Weekly Timeslot", list(report["weekly_timeslot_mapping"])),
		*_reference_inventory("Course Sessions", list(report["course_session_mapping"])),
	]
	if remaining_old_references:
		frappe.throw(_("Old class IDs still have Link references after rename; transaction aborted."))

	if report["weekly_timeslot_mapping"]:
		_advance_series("WTS-2026-", max(int(name.rsplit("-", 1)[1]) for name in report["weekly_timeslot_mapping"].values()))
	if report["course_session_mapping"]:
		_advance_series("CS-2026-", max(int(name.rsplit("-", 1)[1]) for name in report["course_session_mapping"].values()))
	frappe.db.commit()
	result["verification"] = verify(term)
	result["remaining_old_references"] = remaining_old_references
	return result


def _enrollment_integrity(term):
	invalid_timeslots = []
	mismatched_start_sessions = []
	rows = frappe.get_all(
		"Enrollment",
		filters={"term": term},
		fields=["name", "weekly_timeslot", "start_course_session"],
		limit_page_length=0,
	)
	for row in rows:
		if row.get("weekly_timeslot") and not frappe.db.exists("Weekly Timeslot", row.weekly_timeslot):
			invalid_timeslots.append(row.name)
		if row.get("start_course_session"):
			session_timeslot = frappe.db.get_value("Course Sessions", row.start_course_session, "weekly_timeslot")
			if not session_timeslot or session_timeslot != row.get("weekly_timeslot"):
				mismatched_start_sessions.append(row.name)
	return invalid_timeslots, mismatched_start_sessions


def verify(term=SUPPORTED_TERM):
	_require_access()
	_assert_term(term)
	slots, sessions = _rows(term)
	long_slots = [row.name for row in slots if not WTS_PATTERN.match(row.name)]
	long_sessions = [row.name for row in sessions if not SESSION_PATTERN.match(row.name)]
	invalid_session_links = [row.name for row in sessions if not frappe.db.exists("Weekly Timeslot", row.weekly_timeslot)]
	invalid_enrollment_timeslots, mismatched_enrollment_start_sessions = _enrollment_integrity(term)
	return {
		"term": term,
		"weekly_timeslot_count": len(slots),
		"course_session_count": len(sessions),
		"long_weekly_timeslots": long_slots,
		"long_course_sessions": long_sessions,
		"invalid_course_session_links": invalid_session_links,
		"invalid_enrollment_timeslots": invalid_enrollment_timeslots,
		"mismatched_enrollment_start_sessions": mismatched_enrollment_start_sessions,
		"ok": not long_slots and not long_sessions and not invalid_session_links and not invalid_enrollment_timeslots and not mismatched_enrollment_start_sessions and not _duplicate_groups(slots),
	}
