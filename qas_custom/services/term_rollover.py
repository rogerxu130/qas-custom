from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate


TIMESLOT_FIELDS = [
    "term",
    "course",
    "campus",
    "classroom",
    "teacher",
    "day_of_week",
    "start_time",
    "end_time",
]


def copy_term_setup_data(source_term=None, target_term=None, dry_run=1):
    dry_run = cint(dry_run) == 1
    source_term = _clean_name(source_term)
    target_term = _clean_name(target_term)

    _require_system_manager()
    _validate_terms(source_term, target_term)

    target_start_date = frappe.db.get_value("Term", target_term, "start_date")
    source_timeslots = _get_source_timeslots(source_term)

    timeslot_map = {}
    timeslot_results = []
    for source_timeslot in source_timeslots:
        target_timeslot_name = _get_existing_target_timeslot(source_timeslot, target_term)
        action = "reused"

        if not target_timeslot_name:
            action = "would_create" if dry_run else "created"
            target_timeslot_name = _build_target_timeslot_name(source_timeslot, target_term)
            if not dry_run:
                target_timeslot_name = _create_target_timeslot(source_timeslot, target_term)

        timeslot_map[source_timeslot["name"]] = target_timeslot_name
        timeslot_results.append(
            {
                "source": source_timeslot["name"],
                "target": target_timeslot_name,
                "action": action,
                "course": source_timeslot.get("course"),
                "day_of_week": source_timeslot.get("day_of_week"),
                "start_time": str(source_timeslot.get("start_time") or ""),
                "teacher": source_timeslot.get("teacher"),
            }
        )

    enrollment_results = _copy_full_term_enrollments(
        source_term=source_term,
        target_term=target_term,
        target_start_date=target_start_date,
        timeslot_map=timeslot_map,
        dry_run=dry_run,
    )

    if not dry_run:
        frappe.db.commit()

    return {
        "dry_run": dry_run,
        "source_term": source_term,
        "target_term": target_term,
        "weekly_timeslots": _summarize(timeslot_results),
        "enrollments": _summarize(enrollment_results),
        "timeslot_items": timeslot_results,
        "enrollment_items": enrollment_results,
    }


def _require_system_manager():
    if frappe.session.user == "Administrator":
        return

    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only System Manager users can copy term setup."), frappe.PermissionError)


def _validate_terms(source_term: str | None, target_term: str | None):
    if not source_term:
        frappe.throw(_("Source term is required."))
    if not target_term:
        frappe.throw(_("Target term is required."))
    if source_term == target_term:
        frappe.throw(_("Source term and target term must be different."))
    if not frappe.db.exists("Term", source_term):
        frappe.throw(_("Source term {0} was not found.").format(source_term))
    if not frappe.db.exists("Term", target_term):
        frappe.throw(_("Target term {0} was not found.").format(target_term))


def _get_source_timeslots(source_term: str):
    return frappe.get_all(
        "Weekly Timeslot",
        filters={"term": source_term},
        fields=TIMESLOT_FIELDS + ["name"],
        order_by="course asc, campus asc, day_of_week asc, start_time asc, teacher asc",
    )


def _get_existing_target_timeslot(source_timeslot: dict, target_term: str):
    return frappe.db.exists(
        "Weekly Timeslot",
        {
            "term": target_term,
            "course": source_timeslot.get("course"),
            "campus": source_timeslot.get("campus"),
            "classroom": source_timeslot.get("classroom"),
            "teacher": source_timeslot.get("teacher"),
            "day_of_week": source_timeslot.get("day_of_week"),
            "start_time": source_timeslot.get("start_time"),
        },
    )


def _build_target_timeslot_name(source_timeslot: dict, target_term: str):
    parts = [
        target_term,
        source_timeslot.get("course"),
        source_timeslot.get("campus"),
        source_timeslot.get("day_of_week"),
        source_timeslot.get("start_time"),
        source_timeslot.get("teacher"),
    ]
    return "-".join(str(part) for part in parts if part)


def _create_target_timeslot(source_timeslot: dict, target_term: str):
    doc = frappe.new_doc("Weekly Timeslot")
    for fieldname in TIMESLOT_FIELDS:
        doc.set(fieldname, source_timeslot.get(fieldname))
    doc.term = target_term
    doc.insert()
    return doc.name


def _copy_full_term_enrollments(source_term, target_term, target_start_date, timeslot_map, dry_run):
    source_enrollments = frappe.get_all(
        "Enrollment",
        filters={
            "term": source_term,
            "status": "Active",
            "enrollment_type": "Full-Term",
        },
        fields=[
            "name",
            "student",
            "course",
            "weekly_timeslot",
            "enrollment_type",
            "status",
        ],
        order_by="student asc, weekly_timeslot asc",
    )

    results = []
    for source_enrollment in source_enrollments:
        target_timeslot = timeslot_map.get(source_enrollment.get("weekly_timeslot"))
        if not target_timeslot:
            results.append(
                {
                    "source": source_enrollment["name"],
                    "target": None,
                    "action": "skipped_missing_timeslot",
                    "student": source_enrollment.get("student"),
                    "course": source_enrollment.get("course"),
                }
            )
            continue

        existing_enrollment = _get_existing_target_enrollment(
            source_enrollment=source_enrollment,
            target_term=target_term,
            target_timeslot=target_timeslot,
        )
        target_enrollment_name = existing_enrollment.get("name") if existing_enrollment else None
        action = "reused"

        if existing_enrollment and (
            existing_enrollment.get("status") != "Active"
            or existing_enrollment.get("enrollment_type") != "Full-Term"
        ):
            results.append(
                {
                    "source": source_enrollment["name"],
                    "target": target_enrollment_name,
                    "action": "skipped_existing_conflict",
                    "student": source_enrollment.get("student"),
                    "course": source_enrollment.get("course"),
                    "weekly_timeslot": target_timeslot,
                    "existing_status": existing_enrollment.get("status"),
                    "existing_enrollment_type": existing_enrollment.get("enrollment_type"),
                }
            )
            continue

        if not target_enrollment_name:
            action = "would_create" if dry_run else "created"
            target_enrollment_name = _build_target_enrollment_name(source_enrollment, target_timeslot)
            if not dry_run:
                target_enrollment_name = _create_target_enrollment(
                    source_enrollment=source_enrollment,
                    target_term=target_term,
                    target_timeslot=target_timeslot,
                    target_start_date=target_start_date,
                )

        results.append(
            {
                "source": source_enrollment["name"],
                "target": target_enrollment_name,
                "action": action,
                "student": source_enrollment.get("student"),
                "course": source_enrollment.get("course"),
                "weekly_timeslot": target_timeslot,
                "enrollment_date": str(getdate(target_start_date)) if target_start_date else None,
            }
        )

    return results


def _get_existing_target_enrollment(source_enrollment: dict, target_term: str, target_timeslot: str):
    existing_name = frappe.db.exists(
        "Enrollment",
        {
            "student": source_enrollment.get("student"),
            "term": target_term,
            "course": source_enrollment.get("course"),
            "weekly_timeslot": target_timeslot,
        },
    )
    if not existing_name:
        return None

    return frappe.db.get_value(
        "Enrollment",
        existing_name,
        ["name", "status", "enrollment_type"],
        as_dict=True,
    )


def _build_target_enrollment_name(source_enrollment: dict, target_timeslot: str):
    return "-".join(
        str(part)
        for part in [
            source_enrollment.get("student"),
            target_timeslot,
        ]
        if part
    )


def _create_target_enrollment(source_enrollment, target_term, target_timeslot, target_start_date):
    doc = frappe.new_doc("Enrollment")
    doc.student = source_enrollment.get("student")
    doc.term = target_term
    doc.course = source_enrollment.get("course")
    doc.weekly_timeslot = target_timeslot
    doc.enrollment_type = "Full-Term"
    doc.status = "Active"
    doc.enrollment_date = getdate(target_start_date) if target_start_date else None
    doc.insert()
    return doc.name


def _summarize(items: list[dict]):
    summary = {
        "total": len(items),
        "created": 0,
        "would_create": 0,
        "reused": 0,
        "skipped_missing_timeslot": 0,
        "skipped_existing_conflict": 0,
    }
    for item in items:
        action = item.get("action")
        if action in summary:
            summary[action] += 1
    return summary


def _clean_name(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None
