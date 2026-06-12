from __future__ import annotations

from collections import Counter, defaultdict
import json

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, today


SPECIAL_ENROLLMENT_TYPES = {"Trial", "Makeup", "Pay-as-you-go"}


def get_teacher_me_data():
    teacher = _require_teacher()
    return {
        "teacher": _build_teacher_payload(teacher),
    }


def get_teacher_csrf_token_data():
    _require_teacher()
    return {
        "csrf_token": frappe.sessions.get_csrf_token(),
    }


def get_teacher_sessions_data(from_date=None, to_date=None):
    teacher = _require_teacher()
    start_date = getdate(from_date or today())
    end_date = getdate(to_date or add_days(start_date, 14))

    timeslot_rows = _get_teacher_timeslots(teacher.name)
    if not timeslot_rows:
        return {"items": []}

    timeslot_map = {row["name"]: row for row in timeslot_rows}
    session_rows = frappe.get_all(
        "Course Sessions",
        filters={
            "weekly_timeslot": ["in", list(timeslot_map.keys())],
            "session_date": ["between", [start_date, end_date]],
        },
        fields=["name", "weekly_timeslot", "session_date", "status"],
        order_by="session_date asc, modified asc",
    )

    attendance_by_session = _get_attendance_by_session([row["name"] for row in session_rows])
    items = []
    for session in session_rows:
        timeslot = timeslot_map.get(session.get("weekly_timeslot"))
        if not timeslot:
            continue

        attendance_rows = attendance_by_session.get(session["name"], [])
        special_counts = _count_special_students(attendance_rows)
        items.append(
            {
                "id": session["name"],
                "session_id": session["name"],
                "session_date": _as_string(session.get("session_date")),
                "start_time": _as_string(timeslot.get("start_time")),
                "end_time": _as_string(timeslot.get("end_time")),
                "course": timeslot.get("course"),
                "campus": timeslot.get("campus"),
                "classroom": timeslot.get("classroom"),
                "status": session.get("status"),
                "student_count": len(attendance_rows),
                "special_students": special_counts,
            }
        )

    items.sort(key=lambda row: (row.get("session_date") or "", row.get("start_time") or ""))
    return {"items": items}


def get_teacher_session_detail_data(course_session=None):
    teacher = _require_teacher()
    if not course_session:
        frappe.throw(_("Course session is required."))

    session = _get_owned_session(course_session, teacher.name)
    timeslot = _get_timeslot(session.get("weekly_timeslot"))
    attendance_rows = _get_attendance_rows([session["name"]])
    student_map = _get_student_map([row.get("student") for row in attendance_rows if row.get("student")])

    students = []
    for row in sorted(attendance_rows, key=lambda item: item.get("idx") or 0):
        student_id = row.get("student")
        student = student_map.get(student_id, {})
        students.append(
            {
                "row_id": row.get("name"),
                "student": student_id,
                "student_name": student.get("student_name") or student_id,
                "enrollment_type": row.get("enrollment_type"),
                "status": row.get("status"),
                "comments": row.get("comments") or "",
                "makeup_voucher": row.get("makeup_voucher"),
            }
        )

    return {
        "session": {
            "id": session["name"],
            "session_id": session["name"],
            "session_date": _as_string(session.get("session_date")),
            "start_time": _as_string(timeslot.get("start_time")) if timeslot else None,
            "end_time": _as_string(timeslot.get("end_time")) if timeslot else None,
            "course": timeslot.get("course") if timeslot else None,
            "campus": timeslot.get("campus") if timeslot else None,
            "classroom": timeslot.get("classroom") if timeslot else None,
            "status": session.get("status"),
        },
        "students": students,
        "homeworks": _get_homework_rows(session["name"]),
        "status_options": _get_attendance_status_options(),
        "special_students": _count_special_students(attendance_rows),
    }


def update_teacher_attendance_data(course_session=None, updates=None):
    teacher = _require_teacher()
    payload = _get_request_json()
    course_session = course_session or payload.get("course_session")
    updates = updates if updates is not None else payload.get("updates")

    if not course_session:
        frappe.throw(_("Course session is required."))

    session = _get_owned_session(course_session, teacher.name)
    updates = _parse_attendance_updates(updates)
    if not updates:
        frappe.throw(_("No attendance updates were provided."))

    valid_statuses = set(_get_attendance_status_options())
    session_doc = frappe.get_doc("Course Sessions", session["name"])
    attendance_by_row_id = {
        row.name: row
        for row in session_doc.get("attendance_list", [])
    }

    for update in updates:
        row_id = update.get("row_id")
        if not row_id or row_id not in attendance_by_row_id:
            frappe.throw(_("Invalid attendance row."))

        status = (update.get("status") or "").strip()
        if status and status not in valid_statuses:
            frappe.throw(_("Invalid attendance status: {0}").format(status))

        row = attendance_by_row_id[row_id]
        row.status = status or None
        row.comments = (update.get("comments") or "").strip()

    session_doc.save(ignore_permissions=True)
    frappe.db.commit()
    return get_teacher_session_detail_data(course_session=session["name"])


def publish_teacher_homework_data(course_session=None, title=None, description=None):
    teacher = _require_teacher()
    payload = _get_request_json()
    course_session = course_session or payload.get("course_session")
    title = title or payload.get("title")
    description = description if description is not None else payload.get("description")

    if not course_session:
        frappe.throw(_("Course session is required."))

    title = (title or "").strip()
    if not title:
        frappe.throw(_("Homework title is required."))

    session = _get_owned_session(course_session, teacher.name)
    homework = frappe.get_doc(
        {
            "doctype": "Session Homework",
            "course_session": session["name"],
            "title": title,
            "description": (description or "").strip(),
            "status": "Published",
            "teacher": teacher.name,
            "published_at": now_datetime(),
        }
    )
    homework.insert(ignore_permissions=True)
    frappe.db.commit()

    return {
        "homework": {
            "id": homework.name,
            "title": homework.title,
            "description": homework.description or "",
            "course_session": homework.course_session,
            "status": homework.status,
            "published_at": _as_string(homework.published_at),
        }
    }


def _require_teacher():
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required."), frappe.PermissionError)

    teacher_rows = frappe.get_all(
        "Teacher",
        filters={"user": frappe.session.user},
        fields=["name"],
        order_by="modified desc",
    )
    if not teacher_rows:
        frappe.throw(_("No Teacher record is linked to this account."), frappe.PermissionError)

    teacher_name = _resolve_teacher_name([row["name"] for row in teacher_rows])
    return frappe.get_cached_doc("Teacher", teacher_name)


def _resolve_teacher_name(teacher_names: list[str]):
    if len(teacher_names) == 1:
        return teacher_names[0]

    assigned_teacher_names = [
        teacher_name
        for teacher_name in teacher_names
        if frappe.db.exists("Weekly Timeslot", {"teacher": teacher_name})
    ]
    if len(assigned_teacher_names) == 1:
        return assigned_teacher_names[0]

    frappe.throw(
        _("Multiple Teacher records are linked to this account. Please ask an administrator to keep only one active teacher profile."),
        frappe.PermissionError,
    )


def _build_teacher_payload(teacher):
    return {
        "id": teacher.name,
        "name": teacher.get("teacher_name") or teacher.name,
        "user": teacher.get("user"),
    }


def _get_teacher_timeslots(teacher_name: str):
    return frappe.get_all(
        "Weekly Timeslot",
        filters={"teacher": teacher_name},
        fields=[
            "name",
            "course",
            "campus",
            "classroom",
            "teacher",
            "day_of_week",
            "start_time",
            "end_time",
        ],
        order_by="day_of_week asc, start_time asc",
    )


def _get_timeslot(timeslot_name: str | None):
    if not timeslot_name:
        return None

    return frappe.db.get_value(
        "Weekly Timeslot",
        timeslot_name,
        [
            "name",
            "course",
            "campus",
            "classroom",
            "teacher",
            "day_of_week",
            "start_time",
            "end_time",
        ],
        as_dict=True,
    )


def _get_owned_session(course_session: str, teacher_name: str):
    session = frappe.db.get_value(
        "Course Sessions",
        course_session,
        ["name", "weekly_timeslot", "session_date", "status"],
        as_dict=True,
    )
    if not session:
        frappe.throw(_("Course session was not found."))

    timeslot = _get_timeslot(session.get("weekly_timeslot"))
    if not timeslot or timeslot.get("teacher") != teacher_name:
        frappe.throw(_("You do not have access to this course session."), frappe.PermissionError)

    return session


def _get_attendance_rows(session_ids: list[str]):
    if not session_ids:
        return []

    return frappe.get_all(
        "Attendance Record",
        filters={
            "parent": ["in", session_ids],
            "parenttype": "Course Sessions",
            "parentfield": "attendance_list",
        },
        fields=[
            "name",
            "parent",
            "student",
            "enrollment_type",
            "status",
            "comments",
            "makeup_voucher",
            "idx",
        ],
        order_by="parent asc, idx asc",
    )


def _get_attendance_by_session(session_ids: list[str]):
    grouped = defaultdict(list)
    for row in _get_attendance_rows(session_ids):
        grouped[row.get("parent")].append(row)
    return grouped


def _get_student_map(student_ids: list[str]):
    student_ids = sorted({student_id for student_id in student_ids if student_id})
    if not student_ids:
        return {}

    return {
        row["name"]: row
        for row in frappe.get_all(
            "Student",
            filters={"name": ["in", student_ids]},
            fields=["name", "student_name"],
        )
    }


def _get_homework_rows(course_session: str):
    return [
        {
            "id": row.get("name"),
            "title": row.get("title"),
            "description": row.get("description") or "",
            "status": row.get("status"),
            "published_at": _as_string(row.get("published_at")),
        }
        for row in frappe.get_all(
            "Session Homework",
            filters={"course_session": course_session},
            fields=["name", "title", "description", "status", "published_at"],
            order_by="published_at desc, creation desc",
        )
    ]


def _count_special_students(attendance_rows: list[dict]):
    counter = Counter(row.get("enrollment_type") for row in attendance_rows)
    return {
        "trial": counter.get("Trial", 0),
        "makeup": counter.get("Makeup", 0),
        "pay_as_you_go": counter.get("Pay-as-you-go", 0),
    }


def _get_attendance_status_options():
    field = frappe.get_meta("Attendance Record").get_field("status")
    if not field or not field.options:
        return []

    return [option.strip() for option in field.options.splitlines() if option.strip()]


def _parse_attendance_updates(updates):
    if isinstance(updates, str):
        updates = updates.strip()
        if not updates:
            return []
        try:
            updates = json.loads(updates)
        except json.JSONDecodeError:
            frappe.throw(_("Attendance updates must be valid JSON."))

    if isinstance(updates, dict):
        updates = updates.get("updates")

    if not isinstance(updates, list):
        frappe.throw(_("Attendance updates must be a list."))

    return [update for update in updates if isinstance(update, dict)]


def _get_request_json():
    request = getattr(frappe.local, "request", None)
    if not request:
        return {}

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        return {}

    return payload if isinstance(payload, dict) else {}


def _as_string(value):
    if value is None:
        return None
    return str(value)
