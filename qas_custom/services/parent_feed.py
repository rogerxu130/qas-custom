from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.parse import urlsplit

from qas_custom.services.term_media import annotate_media, assert_media_available

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, get_time, get_datetime_in_timezone

from qas_custom.modules.payg.rules import as_brisbane_datetime
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.support_view import get_support_view_parent, get_support_view_token


def get_parent_feed_data(student=None, page=1, page_length=10):
    if frappe.session.user == "Guest" and not get_support_view_parent():
        frappe.throw(_("Please sign in to view the parent feed."))

    page = max(cint(page), 1)
    page_length = max(min(cint(page_length), 20), 1)

    parent_name = get_current_parent_name()
    if not parent_name:
        frappe.throw(_("No Parent record is linked to the current user."))

    students = frappe.get_all(
        "Student",
        filters={"guardian": parent_name},
        fields=["name", "student_name"],
        order_by="modified asc",
    )
    if not students:
        return {"items": [], "page": page, "page_length": page_length, "has_more": False}

    student_map = {row.name: row for row in students}
    student_ids = list(student_map.keys())

    if student:
        if student not in student_map:
            frappe.throw(_("The requested student does not belong to the current parent account."))
        student_ids = [student]

    attendance_rows = _accessible_parent_attendance(parent_name, student_ids=student_ids)

    if not attendance_rows:
        return {"items": [], "page": page, "page_length": page_length, "has_more": False}

    session_student_map = {}
    session_ids = []
    for row in attendance_rows:
        session_student_map.setdefault(row.course_session, set()).add(row.student)
        if row.course_session not in session_ids:
            session_ids.append(row.course_session)

    sessions = frappe.get_all(
        "Course Sessions",
        filters={"name": ["in", session_ids]},
        fields=["name", "weekly_timeslot", "session_date"],
        order_by="session_date desc",
    )
    session_map = {row.name: row for row in sessions}

    weekly_timeslot_ids = list({row.weekly_timeslot for row in sessions if row.weekly_timeslot})
    weekly_timeslot_map = {}
    if weekly_timeslot_ids:
        weekly_timeslot_map = {
            row.name: row
            for row in frappe.get_all(
                "Weekly Timeslot",
                filters={"name": ["in", weekly_timeslot_ids]},
                fields=["name", "course", "day_of_week", "start_time"],
            )
        }

    homework_docs = frappe.get_all(
        "Session Homework",
        filters={"status": "Published", "course_session": ["in", session_ids]},
        fields=["name", "title", "description", "course_session", "published_at", "teacher", "attachments"],
        order_by="published_at desc",
    )

    photo_docs = frappe.get_all(
        "Session Photo Post",
        filters={"status": "Published", "course_session": ["in", session_ids]},
        fields=["name", "title", "caption", "course_session", "posted_at", "teacher"],
        order_by="posted_at desc",
    )

    video_docs = frappe.get_all(
        "Session Video Post",
        filters={"status": "Published", "course_session": ["in", session_ids]},
        fields=[
            "name",
            "title",
            "caption",
            "course_session",
            "posted_at",
            "teacher",
            "video",
            "file_name",
            "file_size",
        ],
        order_by="posted_at desc",
    )

    teacher_ids = {
        row.teacher
        for row in homework_docs + photo_docs + video_docs
        if getattr(row, "teacher", None)
    }
    teacher_map = {}
    if teacher_ids:
        teacher_map = {
            row.name: row.teacher_name
            for row in frappe.get_all(
                "Teacher",
                filters={"name": ["in", list(teacher_ids)]},
                fields=["name", "teacher_name"],
            )
        }

    photo_ids = [row.name for row in photo_docs]
    photo_item_rows = []
    if photo_ids:
        photo_item_rows = frappe.get_all(
            "Session Photo Item",
            filters={
                "parent": ["in", photo_ids],
                "parenttype": "Session Photo Post",
                "parentfield": "photos",
            },
            fields=["parent", "image", "idx"],
            order_by="parent asc, idx asc",
        )

    photo_media_map = {}
    for row in photo_item_rows:
        if not row.image:
            continue
        photo_media_map.setdefault(row.parent, []).append({"idx": row.idx, "image": row.image})

    items = []
    for row in homework_docs:
        items.append(
            _build_homework_item(
                row=row,
                session_map=session_map,
                weekly_timeslot_map=weekly_timeslot_map,
                student_map=student_map,
                session_student_map=session_student_map,
                teacher_map=teacher_map,
            )
        )

    for row in photo_docs:
        items.append(
            _build_photo_item(
                row=row,
                session_map=session_map,
                weekly_timeslot_map=weekly_timeslot_map,
                student_map=student_map,
                session_student_map=session_student_map,
                teacher_map=teacher_map,
                photo_media_map=photo_media_map,
            )
        )

    for row in video_docs:
        items.append(
            _build_video_item(
                row=row,
                session_map=session_map,
                weekly_timeslot_map=weekly_timeslot_map,
                student_map=student_map,
                session_student_map=session_student_map,
                teacher_map=teacher_map,
            )
        )

    items.sort(key=lambda item: item["published_at"] or "", reverse=True)

    start = (page - 1) * page_length
    end = start + page_length
    paged_items = annotate_media(items[start:end])

    return {
        "items": paged_items,
        "page": page,
        "page_length": page_length,
        "has_more": len(items) > end,
    }


def _build_homework_item(row, session_map, weekly_timeslot_map, student_map, session_student_map, teacher_map):
    session = session_map.get(row.course_session)
    timeslot = weekly_timeslot_map.get(session.weekly_timeslot) if session else None
    return {
        "type": "homework",
        "id": row.name,
        "title": row.title or "Homework",
        "summary": row.description or "",
        "course_session": row.course_session,
        "course_name": timeslot.course if timeslot else None,
        "session_label": _build_session_label(timeslot),
        "session_date": str(session.session_date) if session and session.session_date else None,
        "published_at": _normalize_datetime(row.published_at),
        "teacher": _build_teacher_payload(row.teacher, teacher_map),
        "students": _build_students_payload(row.course_session, session_student_map, student_map),
        "attachments": _build_homework_attachments(row.name, row.attachments),
        "route": f"/updates/homework/{row.name}",
    }


def _build_photo_item(row, session_map, weekly_timeslot_map, student_map, session_student_map, teacher_map, photo_media_map):
    session = session_map.get(row.course_session)
    timeslot = weekly_timeslot_map.get(session.weekly_timeslot) if session else None
    media = photo_media_map.get(row.name, [])
    photos = [_build_photo_asset(row.name, photo) for photo in media if photo.get("image")]
    return {
        "type": "photo_post",
        "id": row.name,
        "title": row.title or "Class Photos",
        "summary": row.caption or "",
        "course_session": row.course_session,
        "course_name": timeslot.course if timeslot else None,
        "session_label": _build_session_label(timeslot),
        "session_date": str(session.session_date) if session and session.session_date else None,
        "published_at": _normalize_datetime(row.posted_at),
        "teacher": _build_teacher_payload(row.teacher, teacher_map),
        "students": _build_students_payload(row.course_session, session_student_map, student_map),
        "photo_count": len(photos),
        "cover_image": photos[0] if photos else None,
        "photos": photos,
        "route": f"/updates/photos/{row.name}",
    }


def _build_video_item(row, session_map, weekly_timeslot_map, student_map, session_student_map, teacher_map):
    session = session_map.get(row.course_session)
    timeslot = weekly_timeslot_map.get(session.weekly_timeslot) if session else None
    return {
        "type": "video_post",
        "id": row.name,
        "title": row.title or "Class Video",
        "summary": row.caption or "",
        "course_session": row.course_session,
        "course_name": timeslot.course if timeslot else None,
        "session_label": _build_session_label(timeslot),
        "session_date": str(session.session_date) if session and session.session_date else None,
        "published_at": _normalize_datetime(row.posted_at),
        "teacher": _build_teacher_payload(row.teacher, teacher_map),
        "students": _build_students_payload(row.course_session, session_student_map, student_map),
        "video": _build_video_asset(row),
        "route": f"/updates/videos/{row.name}",
    }


def _build_students_payload(session_id, session_student_map, student_map):
    student_ids = sorted(session_student_map.get(session_id, set()))
    return [
        {"id": student_id, "name": student_map[student_id].student_name or student_id}
        for student_id in student_ids
        if student_id in student_map
    ]


def _build_teacher_payload(teacher_id, teacher_map):
    if not teacher_id:
        return None
    return {"id": teacher_id, "name": teacher_map.get(teacher_id) or teacher_id}


def _build_homework_attachments(homework_name, attachment_url):
    if not attachment_url:
        return []
    label = attachment_url.split("/")[-1]
    return [{"label": label, "url": _media_proxy_url("parent_portal_get_feed_homework", homework=homework_name)}]


def _build_session_label(timeslot):
    if not timeslot:
        return None

    parts = [timeslot.day_of_week]
    if timeslot.start_time:
        parts.append(str(timeslot.start_time))
    return " ".join(part for part in parts if part)


def _normalize_datetime(value):
    if not value:
        return None
    return str(get_datetime(value))


def _media_proxy_url(endpoint, **params):
    token = get_support_view_token()
    if token:
        params["support_token"] = token
    return "/api/method/qas_custom.api.parent_portal." + endpoint + "?" + urlencode(params)


def _build_photo_asset(photo_post_name, photo_row):
    idx = cint(photo_row.get("idx"))
    return _media_proxy_url("parent_portal_get_feed_photo", photo_post=photo_post_name, photo_idx=idx)


def _build_video_asset(row):
    if not row.video:
        return None

    video_url = _media_proxy_url("parent_portal_get_feed_video", video_post=row.name)
    video_download_url = _media_proxy_url("parent_portal_get_feed_video", video_post=row.name, download=1)

    return {
        "url": video_url,
        "download_url": video_download_url,
        "file_name": row.file_name or row.video.rsplit("/", 1)[-1],
        "file_size": row.file_size or 0,
    }


def get_current_parent_name():
    support_parent = get_support_view_parent()
    if support_parent:
        return support_parent.name
    return frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")


def _require_parent():
    if frappe.session.user == "Guest":
        frappe.throw(_("Please sign in to view class media."))

    parent_name = get_current_parent_name()
    if not parent_name:
        frappe.throw(_("No Parent record is linked to the current user."))

    return parent_name


def _accessible_parent_attendance(parent_name, course_session=None, student_ids=None):
    """Keep legacy attendance access; require proven participation for PAYG rows."""
    owned_students = frappe.get_all("Student", filters={"guardian": parent_name}, pluck="name")
    selected = list(set(owned_students) & set(student_ids)) if student_ids is not None else owned_students
    if not selected:
        return []
    filters = {"student": ["in", selected]}
    if course_session:
        filters["course_session"] = course_session
    rows = frappe.get_all(ATTENDANCE_DOCTYPE, filters=filters,
                          fields=["name", "course_session", "student", "status",
                                  "source_doctype", "source_document"],
                          order_by="course_session asc")
    if not rows:
        return []
    bookings = frappe.get_all("QAS PAYG Booking",
                              filters={"attendance_entry": ["in", [row.name for row in rows]]},
                              fields=["name", "attendance_entry", "student", "course_session", "status"])
    booking_by_attendance = {booking.attendance_entry: booking for booking in bookings}
    payg_sessions = {row.course_session for row in rows
                     if row.source_doctype == "QAS PAYG Booking" or row.name in booking_by_attendance}
    finished_sessions = _payg_finished_sessions(payg_sessions) if payg_sessions else set()
    allowed = []
    for row in rows:
        booking = booking_by_attendance.get(row.name)
        if row.source_doctype == "QAS PAYG Booking" or booking:
            if (booking and row.source_doctype == "QAS PAYG Booking" and
                    row.source_document == booking.name and booking.attendance_entry == row.name and
                    booking.student == row.student and booking.course_session == row.course_session and
                    booking.status in ("Locked", "Completed") and row.status in ("Present", "Late") and
                    row.course_session in finished_sessions):
                allowed.append(row)
        else:
            allowed.append(row)
    return allowed


def _payg_finished_sessions(session_ids):
    if not session_ids:
        return set()
    sessions = frappe.get_all("Course Sessions", filters={"name": ["in", sorted(session_ids)]},
                              fields=["name", "session_date", "weekly_timeslot"])
    slot_ids = {row.weekly_timeslot for row in sessions if row.weekly_timeslot}
    slots = frappe.get_all("Weekly Timeslot", filters={"name": ["in", sorted(slot_ids)]},
                           fields=["name", "start_time", "end_time"]) if slot_ids else []
    slot_map = {row.name: row for row in slots}
    now = as_brisbane_datetime(get_datetime_in_timezone("Australia/Brisbane"))
    finished = set()
    for session in sessions:
        slot = slot_map.get(session.weekly_timeslot)
        if not session.session_date or not slot or not slot.end_time:
            continue
        end = datetime.combine(getdate(session.session_date), get_time(slot.end_time))
        if slot.start_time and get_time(slot.end_time) <= get_time(slot.start_time):
            end += timedelta(days=1)
        if as_brisbane_datetime(end) <= now:
            finished.add(session.name)
    return finished


def _validate_parent_session_access(parent_name, course_session):
    if not course_session:
        raise frappe.PermissionError
    if not _accessible_parent_attendance(parent_name, course_session=course_session):
        raise frappe.PermissionError


def get_parent_feed_photo_content(photo_post, photo_idx):
    parent_name = _require_parent()

    photo_post_doc = frappe.get_doc("Session Photo Post", photo_post)
    _require_published_media_doc(photo_post_doc)
    course_session = photo_post_doc.get("course_session")
    _validate_parent_session_access(parent_name, course_session)

    target_idx = cint(photo_idx)
    if target_idx <= 0:
        raise frappe.PermissionError

    assert_media_available(photo_post_doc.name, "photo", target_idx)
    photo_row = next((row for row in photo_post_doc.photos or [] if cint(row.idx) == target_idx), None)
    if not photo_row or not getattr(photo_row, "image", None):
        raise frappe.DoesNotExistError

    return _authorized_media(photo_row.image)


def get_parent_feed_video_content(video_post, download=False):
    parent_name = _require_parent()

    video_post_doc = frappe.get_doc("Session Video Post", video_post)
    _require_published_media_doc(video_post_doc)
    _validate_parent_session_access(parent_name, video_post_doc.get("course_session"))

    assert_media_available(video_post_doc.name, "video")
    if not video_post_doc.video:
        raise frappe.DoesNotExistError

    return _authorized_media(video_post_doc.video, file_name=video_post_doc.file_name,
                             content_type=video_post_doc.mime_type,
                             display_content_as="attachment" if cint(download) else "inline")


def get_parent_feed_homework_content(homework):
    parent_name = _require_parent()
    doc = frappe.get_doc("Session Homework", homework)
    _require_published_media_doc(doc)
    _validate_parent_session_access(parent_name, doc.get("course_session"))
    if not doc.attachments:
        raise frappe.DoesNotExistError
    return _authorized_media(doc.attachments, display_content_as="attachment")


def _require_published_media_doc(doc):
    if doc.get("status") != "Published":
        raise frappe.PermissionError


def _authorized_media(saved_url, file_name=None, content_type=None, display_content_as="inline"):
    parsed = urlsplit(saved_url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return {"redirect": saved_url}
    if parsed.scheme or parsed.netloc or not saved_url.startswith(("/files/", "/private/files/")):
        raise frappe.PermissionError
    file_doc_name = frappe.db.get_value("File", {"file_url": saved_url}, "name")
    if not file_doc_name:
        raise frappe.DoesNotExistError
    file_doc = frappe.get_doc("File", file_doc_name)
    filename = file_doc.file_name or file_name or saved_url.rsplit("/", 1)[-1]
    return {"filename": filename, "content": file_doc.get_content(),
            "content_type": content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "display_content_as": display_content_as}


def cint(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
