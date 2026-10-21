"""A class has a current timetable; each session keeps its actual teaching times."""
from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
from collections.abc import Mapping

import frappe
from frappe import _
from frappe.utils import getdate, get_time, now_datetime


TIME_FIELDS = ("start_time", "end_time")
SCHEDULE_FIELDS = ("day_of_week", *TIME_FIELDS)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def session_timeslot(session, timeslot):
    """Return a copy: never change a shared class map while rendering a session."""
    if not timeslot:
        return timeslot
    source = timeslot.as_dict() if callable(getattr(timeslot, "as_dict", None)) else timeslot
    result = frappe._dict(source if isinstance(source, Mapping) else vars(source))
    if session is not None and not callable(getattr(session, "get", None)):
        session = vars(session)
    for field in TIME_FIELDS:
        if session and session.get(field) not in (None, ""):
            result[field] = session.get(field)
    if session and session.get("session_date"):
        result.day_of_week = DAYS[getdate(session.get("session_date")).weekday()]
    return result


def initialise_session_times(doc):
    if all(doc.get(field) not in (None, "") for field in TIME_FIELDS):
        return
    # Existing sessions must retain their saved times even when other fields change.
    previous = doc.get_doc_before_save() if not doc.is_new() else None
    source = previous or frappe.db.get_value(
        "Weekly Timeslot", doc.get("weekly_timeslot"), list(TIME_FIELDS), as_dict=True
    ) or {}
    for field in TIME_FIELDS:
        if doc.get(field) in (None, ""):
            doc.set(field, source.get(field))


def preserve_session_times(timeslot):
    """Fill missing times with the OLD timetable, before saving any class edit."""
    frappe.db.sql("""
        UPDATE `tabCourse Sessions`
        SET start_time = COALESCE(start_time, %s), end_time = COALESCE(end_time, %s)
        WHERE weekly_timeslot = %s AND (start_time IS NULL OR end_time IS NULL)
    """, (timeslot.get("start_time"), timeslot.get("end_time"), timeslot.name))


def guard_weekly_schedule_change(doc, method=None):
    previous = doc.get_doc_before_save()
    if not previous or not any(
        (_time(doc.get(f)) != _time(previous.get(f))) if f in TIME_FIELDS
        else doc.get(f) != previous.get(f) for f in SCHEDULE_FIELDS
    ):
        return
    if frappe.db.exists("Course Sessions", {"weekly_timeslot": doc.name}):
        if not doc.flags.get("reviewed_schedule_change"):
            frappe.throw(_("Use the School Admin schedule change action and select an effective date."))
        preserve_session_times(previous)


def _time(value):
    if value in (None, ""):
        frappe.throw(_("Start and end times are required."))
    return get_time(value)


def _schedule(value):
    return {"day_of_week": value.get("day_of_week"), **{f: str(_time(value.get(f))) for f in TIME_FIELDS}}


def plan_schedule_change(old, new, sessions, locked, effective_date, term, *, now=None):
    """Pure planning logic shared by preview and execution."""
    now = now or now_datetime()
    if not effective_date:
        frappe.throw(_("Effective date is required."))
    effective = getdate(effective_date)
    if effective < now.date():
        frappe.throw(_("Effective date cannot be in the past."))
    if term.get("status") in ("Closed", "Archived"):
        frappe.throw(_("Reopen the term before changing its schedule."))
    if not term.get("start_date") or not term.get("end_date"):
        frappe.throw(_("The term must have start and end dates."))
    if not getdate(term.get("start_date")) <= effective <= getdate(term.get("end_date")):
        frappe.throw(_("Effective date must be within the term."))
    before, after = _schedule(old), _schedule(new)
    if after["day_of_week"] not in DAYS:
        frappe.throw(_("Choose a valid weekday."))
    if _time(after["end_time"]) <= _time(after["start_time"]):
        frappe.throw(_("End time must be after start time."))
    changes, retained, errors = [], [], []
    weekday_changed = before["day_of_week"] != after["day_of_week"]
    target = effective + timedelta(days=(DAYS.index(after["day_of_week"]) - effective.weekday()) % 7)
    for session in sorted(sessions, key=lambda s: (str(s.get("session_date")), s.get("name"))):
        actual = session_timeslot(session, old)
        old_date = getdate(session.get("session_date"))
        reason = None
        if old_date < effective:
            reason = "Before effective date"
        elif session.get("status") != "Scheduled":
            reason = session.get("status") or "Not scheduled"
        elif session.get("name") in locked:
            reason = "Attendance has already been marked"
        elif datetime.combine(old_date, _time(actual.get("start_time"))) <= now:
            reason = "Session has already started"
        # A locked scheduled session still occupies its position in the sequence.
        new_date = target if weekday_changed else old_date
        if old_date >= effective and session.get("status") == "Scheduled" and weekday_changed:
            target += timedelta(days=7)
        item = {"session": session.get("name"), "old_date": str(old_date),
                "old_start_time": str(_time(actual.get("start_time"))),
                "old_end_time": str(_time(actual.get("end_time")))}
        if reason:
            retained.append({**item, "reason": reason})
            continue
        if new_date > getdate(term.get("end_date")):
            errors.append(f"{session.get('name')}: target date {new_date} is after the term ends.")
        if datetime.combine(new_date, _time(after["start_time"])) <= now:
            errors.append(f"{session.get('name')}: target time has already passed.")
        if (str(new_date), after["start_time"], after["end_time"]) != (str(old_date), item["old_start_time"], item["old_end_time"]):
            changes.append({**item, "session_date": str(new_date),
                            "start_time": after["start_time"], "end_time": after["end_time"]})
    moving = {row["session"] for row in changes}
    occupied = {str(getdate(row.get("session_date"))) for row in sessions if row.get("name") not in moving}
    for row in changes:
        if row["session_date"] in occupied:
            errors.append(f"{row['session']}: {row['session_date']} already has a retained or cancelled session.")
        occupied.add(row["session_date"])
    return {"before": before, "after": after, "effective_date": str(effective),
            "changes": changes, "retained": retained, "blocking_errors": errors,
            "updated": len(changes), "skipped": sum(r["reason"] != "Before effective date" for r in retained)}


def build_schedule_preview(doc, payload):
    if payload.get("term", doc.get("term")) != doc.get("term"):
        frappe.throw(_("A schedule change must keep the class in its original term."))
    term = frappe.db.get_value("Term", doc.get("term"), ["start_date", "end_date", "status"], as_dict=True) or {}
    sessions = frappe.get_all("Course Sessions", filters={"weekly_timeslot": doc.name},
        fields=["name", "session_date", "status", "start_time", "end_time", "teacher_override", "modified"],
        order_by="session_date asc, name asc", limit_page_length=0)
    locked = set(frappe.get_all("Class Attendance Entry",
        filters={"course_session": ["in", [r.name for r in sessions]], "status": ["not in", ["To be started", "Cancelled"]]},
        pluck="course_session", limit_page_length=0)) if sessions else set()
    new = frappe._dict(doc.as_dict())
    new.update({key: value for key, value in payload.items() if key in SCHEDULE_FIELDS + ("campus", "classroom", "teacher")})
    result = plan_schedule_change(doc, new, sessions, locked, payload.get("effective_date"), term)
    _check_resource_conflicts(new, sessions, result)
    result["pass_bookings"] = _check_pass_booking_dates(result)
    identity = {"class": doc.as_dict(), "payload": payload, "sessions": sessions,
                "locked": sorted(locked), "plan": result}
    identity["payload"] = {k: v for k, v in payload.items() if k != "schedule_confirmation_token"}
    result["confirmation_token"] = sha256(json.dumps(identity, default=str, sort_keys=True).encode()).hexdigest()
    return result


def _check_pass_booking_dates(plan):
    changes = {row["session"]: row for row in plan["changes"]}
    if not changes:
        return []
    bookings = frappe.get_all("QAS PAYG Booking",
        filters={"course_session": ["in", list(changes)], "status": ["in", ["Reserved", "Locked"]]},
        fields=["name", "course_session", "card", "status", "modified"], limit_page_length=0)
    cards = frappe.get_all("QAS PAYG Card", filters={"name": ["in", sorted({r.card for r in bookings})]},
        fields=["name", "expires_on", "modified"], limit_page_length=0) if bookings else []
    card_map = {row.name: row for row in cards}
    for booking in bookings:
        card = card_map.get(booking.card)
        if not card or not card.get("expires_on") or getdate(changes[booking.course_session]["session_date"]) > getdate(card.expires_on):
            plan["blocking_errors"].append(f"{booking.course_session}: booking {booking.name} would fall outside its ten-class pass expiry. Review the card expiry first.")
    return [{**dict(row), "card_expiry": (card_map.get(row.card) or {}).get("expires_on")} for row in bookings]


def _check_resource_conflicts(new, sessions, plan):
    if not plan["changes"]:
        return
    dates = sorted({r["session_date"] for r in plan["changes"]})
    other = frappe.get_all("Course Sessions",
        filters={"session_date": ["in", dates], "status": ["!=", "Cancelled"], "weekly_timeslot": ["!=", new.name]},
        fields=["name", "weekly_timeslot", "session_date", "start_time", "end_time", "teacher_override"], limit_page_length=0)
    slots = frappe.get_all("Weekly Timeslot", filters={"name": ["in", sorted({r.weekly_timeslot for r in other})]},
        fields=["name", "classroom", "teacher", "start_time", "end_time"], limit_page_length=0) if other else []
    slot_map = {r.name: r for r in slots}
    source = {r.name: r for r in sessions}
    for change in plan["changes"]:
        teacher = source[change["session"]].get("teacher_override") or new.get("teacher")
        for row in other:
            if str(getdate(row.session_date)) != change["session_date"]:
                continue
            actual = session_timeslot(row, slot_map.get(row.weekly_timeslot))
            if not actual:
                continue
            room_conflict = new.get("classroom") and actual.get("classroom") == new.get("classroom")
            teacher_conflict = teacher and teacher == (row.get("teacher_override") or actual.get("teacher"))
            if (room_conflict or teacher_conflict) and _time(change["start_time"]) < _time(actual.get("end_time")) and _time(actual.get("start_time")) < _time(change["end_time"]):
                plan["blocking_errors"].append(f"{change['session']}: room or teacher conflicts with {row.name} on {change['session_date']}.")


def apply_session_changes(plan):
    from qas_custom.services.class_record_labels import build_course_session_label
    for change in plan["changes"]:
        name = change["session"]
        values = {field: change[field] for field in ("session_date", *TIME_FIELDS)}
        frappe.db.set_value("Course Sessions", name, values)
        session = frappe.get_doc("Course Sessions", name)
        frappe.db.set_value("Course Sessions", name, "display_label", build_course_session_label(session), update_modified=False)
        # Keep appointment/search fields and PAYG cancellation deadlines aligned with the linked session.
        for inquiry in frappe.get_all("Inquiry", filters={"course_session": name}, pluck="name", limit_page_length=0):
            frappe.db.set_value("Inquiry", inquiry, {"current_appointment_date": change["session_date"], "current_appointment_time": change["start_time"]})
        for booking in frappe.get_all("Adhoc Booking", filters={"course_session": name, "status": ["in", ["Reserved", "Locked"]]}, pluck="name", limit_page_length=0):
            deadline = datetime.combine(getdate(change["session_date"]), _time(change["start_time"])) - timedelta(hours=72)
            frappe.db.set_value("Adhoc Booking", booking, {"class_date": change["session_date"], "start_time": change["start_time"], "end_time": change["end_time"], "cancellable_until": deadline})
        for booking in frappe.get_all("QAS PAYG Booking", filters={"course_session": name, "status": ["in", ["Reserved", "Locked"]]}, pluck="name", limit_page_length=0):
            deadline = datetime.combine(getdate(change["session_date"]), _time(change["start_time"])) - timedelta(hours=72)
            frappe.db.set_value("QAS PAYG Booking", booking, "cancellable_until", deadline)
        for leave in frappe.get_all("Leave Request", filters={"course_session": name}, pluck="name", limit_page_length=0):
            frappe.db.set_value("Leave Request", leave, "session_date", change["session_date"])
        frappe.clear_document_cache("Course Sessions", name)
