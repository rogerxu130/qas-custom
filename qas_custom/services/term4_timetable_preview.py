"""Read-only review of the approved Term 4 timetable proposal. No execute entrypoint."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import timedelta
from itertools import combinations
from pathlib import Path

import frappe
from frappe.utils import getdate, today

from qas_custom.services.term4_class_id_migration import SUPPORTED_TERM, _link_fields, _require_access

FIELDS = ("course", "class_language", "campus", "classroom", "day_of_week", "start_time", "end_time", "teacher", "status")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
EDIT_ACTIONS = {"update", "delete_candidate"}


def _minutes(value):
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds() // 60)
    parts = str(value).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _normal(row):
    result = {field: row.get(field) or "" for field in FIELDS}
    for field in ("start_time", "end_time"):
        if result[field] != "":
            minutes = _minutes(result[field])
            result[field] = f"{minutes // 60:02d}:{minutes % 60:02d}:00"
    return result


def _dates(term, weekday):
    start, end = getdate(term["start_date"]), getdate(term["end_date"])
    start += timedelta(days=(DAYS.index(weekday) - start.weekday()) % 7)
    result = []
    while start <= end:
        result.append(str(start))
        start += timedelta(days=7)
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _rows(doctype, filters, fields):
    return frappe.get_all(doctype, filters=filters, fields=fields, order_by="name asc", limit_page_length=0)


def _references(target, names):
    """Inventory Link and Dynamic Link rows, without touching their documents."""
    if not names:
        return []
    result = []

    def collect(doctype, field, filters, dynamic=False):
        meta = frappe.get_meta(doctype)
        # Singles have no ordinary table; read their stored fields explicitly.
        if meta.issingle:
            values = frappe.db.get_singles_dict(doctype)
            if values.get(field) not in names:
                return
            if dynamic and values.get(filters[0][0]) != target:
                return
            rows = [{"name": doctype, field: values[field]}]
        else:
            fields = ["name", field]
            fields += [f for f in ("modified", "status", "student", "enrollment_type", "inquiry_type", "invoice", "invoice_status", "invoice_amount", "start_course_session", "source_doctype", "source_document") if meta.has_field(f) and f not in fields]
            rows = _rows(doctype, filters, fields)
        for row in rows:
            result.append({"doctype": doctype, "field": field, "target": row[field], "dynamic": dynamic, "record": dict(row)})

    for doctype, field in _link_fields(target):
        collect(doctype, field, {field: ["in", names]})
    seen = set()
    for schema, owner in (("DocField", "parent"), ("Custom Field", "dt")):
        for row in frappe.get_all(schema, filters={"fieldtype": "Dynamic Link"}, fields=[owner, "fieldname", "options"], limit_page_length=0):
            key = (row[owner], row.fieldname, row.options)
            if key in seen:
                continue
            seen.add(key)
            if row.options:
                collect(row[owner], row.fieldname, [[row.options, "=", target], [row.fieldname, "in", names]], dynamic=True)
    return sorted(result, key=lambda r: (r["doctype"], r["field"], r["record"]["name"]))


def _label(row, rooms):
    room = rooms.get(row.get("classroom"), {}).get("classroom_name") or row.get("classroom")
    return " · ".join(str(v) for v in [row.get("campus"), room, row.get("day_of_week"), row.get("start_time"), row.get("course"), row.get("teacher") or "Unassigned"] if v)


def _review(plan, snapshot, as_of):
    blockers, warnings, reports = [], [], []
    slots = {r["name"]: r for r in snapshot["slots"]}
    rooms = {r["name"]: r for r in snapshot["rooms"]}
    teachers = {r["name"]: r for r in snapshot["teachers"]}
    courses = {r["name"]: r for r in snapshot["courses"]}
    term = snapshot["term"]
    if not term or term.get("status") != "Upcoming" or not term.get("start_date") or not term.get("end_date"):
        blockers.append("Term must be Upcoming with valid start and end dates.")
    elif getdate(term["start_date"]) <= getdate(as_of) or getdate(term["end_date"]) < getdate(term["start_date"]):
        blockers.append("Term dates are invalid or the term has already started.")
    source_names = [r["source"] for r in plan["actions"] if r["source"]]
    if len(source_names) != len(set(source_names)) or set(source_names) != set(slots):
        blockers.append("Live class set differs from the reviewed 135-class baseline, or the plan maps a class twice.")
    sessions = defaultdict(list)
    for row in snapshot["sessions"]:
        sessions[row["weekly_timeslot"]].append(row)
    references = defaultdict(list)
    for row in snapshot["references"]:
        references[row["target"]].append(row)
    projected = []
    calendar_valid = bool(term and term.get("start_date") and term.get("end_date"))
    for action in plan["actions"]:
        source, target = action["source"], action["target"]
        current = slots.get(source)
        errors = []
        title = _label(target or current or action["expected"], rooms)
        if source and (not current or _normal(current) != action["expected"]):
            errors.append("Live class fields changed since the reviewed baseline.")
        if target:
            projected.append(target)
            course, room = courses.get(target["course"]), rooms.get(target["classroom"])
            teacher = teachers.get(target["teacher"]) if target["teacher"] else None
            if not course or not course.get("duration_mins"):
                errors.append("Course or duration is missing.")
            elif _minutes(target["end_time"]) - _minutes(target["start_time"]) != int(course["duration_mins"]):
                errors.append("Planned duration differs from the live Course duration.")
            if not room or room.get("status") != "Active" or room.get("campus") != target["campus"]:
                errors.append("Classroom is missing, inactive, or belongs to a different campus.")
            if target["teacher"] and (not teacher or teacher.get("status") != "Active"):
                errors.append("Teacher is missing or inactive.")
        children = sessions[source] if source else []
        linked = list(references[source]) if source else []
        for session in children:
            linked.extend(references[session["name"]])
        external = [r for r in linked if not (r["doctype"] == "Course Sessions" and r["field"] == "weekly_timeslot")]
        enrollment_refs = [r for r in external if r["doctype"] == "Enrollment" and r["field"] == "weekly_timeslot"]
        changed = {f: {"before": action["expected"][f], "after": target[f]} for f in FIELDS if target and action["expected"] and target[f] != action["expected"][f]}
        if action["action"] == "delete_candidate" and external:
            errors.append("Deletion candidate has live references; preserve it pending review.")
        if "course" in changed and external:
            errors.append("Course change has enrollments or other references; student review is required.")
        if action["action"] in EDIT_ACTIONS:
            if any(s.get("status") != "Scheduled" or not s.get("session_date") or getdate(s["session_date"]) <= getdate(as_of) for s in children):
                errors.append("An affected session is not future Scheduled; manual review required.")
            if any(r["doctype"] == "Class Attendance Entry" and r["record"].get("status") not in ("To be started", "Scheduled", "Not Marked", "Cancelled") for r in external):
                errors.append("Affected attendance has already been marked.")
            if any(s.get("teacher_override") for s in children):
                errors.append("Affected sessions have teacher overrides; review before applying a class-wide change.")
        planned_dates = _dates(term, target["day_of_week"]) if target and calendar_valid else []
        old_dates = sorted(str(s["session_date"]) for s in children)
        if source and action["action"] in EDIT_ACTIONS and calendar_valid:
            if old_dates != _dates(term, action["expected"]["day_of_week"]):
                errors.append("Existing session dates differ from the term weekly calendar; preserve exceptions for review.")
        if "day_of_week" in changed:
            errors.append("Weekday changes require an explicit per-session date mapping, which this plan does not provide.")
        if action["action"] == "hold_referenced":
            warnings.append(f"Retained pending trial review: {title}")
        if enrollment_refs and any(f in changed for f in ("teacher", "classroom", "start_time", "end_time", "campus")):
            warnings.append(f"Notify enrolled families about the approved schedule/teacher change: {title}")
        report = {
            "action": action["action"], "class": title, "source": source,
            "before": _normal(current) if current else None, "after": target, "changes": changed,
            "session_count": len(children), "session_dates": old_dates,
            "new_session_dates": planned_dates if action["action"] == "create_candidate" else [],
            "session_ids": [s["name"] for s in children] if action["action"] in EDIT_ACTIONS else [],
            "reference_counts": dict(Counter(r["doctype"] + "." + r["field"] for r in external)),
            "reference_records": external if action["action"] != "unchanged" else [],
            "blocking_errors": errors,
        }
        if action["action"] != "unchanged" or errors:
            reports.append(report)
        blockers.extend(f"{title}: {e}" for e in errors)
    for left, right in combinations(projected, 2):
        if left["status"] != "Active" or right["status"] != "Active" or left["day_of_week"] != right["day_of_week"]:
            continue
        if max(_minutes(left["start_time"]), _minutes(right["start_time"])) >= min(_minutes(left["end_time"]), _minutes(right["end_time"])):
            continue
        if left["classroom"] == right["classroom"] or (left["teacher"] and left["teacher"] == right["teacher"]):
            blockers.append(f"Final room/teacher overlap: {_label(left, rooms)} / {_label(right, rooms)}")
    summary = dict(Counter(r["action"] for r in plan["actions"]))
    summary.update({"live_classes": len(slots), "live_sessions": len(snapshot["sessions"]), "proposed_classes": len(projected), "sessions_to_remove": sum(r["session_count"] for r in reports if r["action"] == "delete_candidate"), "sessions_to_create": sum(len(r["new_session_dates"]) for r in reports)})
    return {
        "term": SUPPORTED_TERM, "read_only": True, "executable": False,
        "summary": summary, "actions": reports, "blocking_errors": blockers,
        "warnings": warnings + ["No automatic invoice, student attendance, or inquiry changes are authorized by this preview.", "Conflict checks cover this term only; travel time, other terms and free-text references are not checked.", "The Wednesday UMG R2 teacher change is deferred: Clara remains while Amaya's R5 trial class is retained."],
        "review_token": _fingerprint({"plan": plan, "snapshot": snapshot, "as_of": str(as_of)}),
    }


def preview():
    _require_access()
    plan = json.loads(Path(__file__).with_name("term4_timetable_plan.json").read_text())
    if plan["term"] != SUPPORTED_TERM:
        frappe.throw("The timetable plan must be restricted to Term 4 2026.")
    slots = _rows("Weekly Timeslot", {"term": SUPPORTED_TERM}, ["name", "modified", *FIELDS])
    names = [r.name for r in slots]
    session_fields = ["name", "weekly_timeslot", "session_date", "status", "modified"]
    if frappe.get_meta("Course Sessions").has_field("teacher_override"):
        session_fields.append("teacher_override")
    sessions = _rows("Course Sessions", {"weekly_timeslot": ["in", names]}, session_fields) if names else []
    snapshot = {
        "term": frappe.db.get_value("Term", SUPPORTED_TERM, ["name", "start_date", "end_date", "status", "modified"], as_dict=True),
        "slots": slots, "sessions": sessions,
        "references": _references("Weekly Timeslot", names) + _references("Course Sessions", [s.name for s in sessions]),
        "rooms": _rows("Classroom", {}, ["name", "classroom_name", "campus", "status", "capacity", "modified"]),
        "teachers": _rows("Teacher", {}, ["name", "teacher_name", "status", "modified"]),
        "courses": _rows("Course", {}, ["name", "duration_mins", "modified"]),
    }
    return _review(plan, snapshot, today())
