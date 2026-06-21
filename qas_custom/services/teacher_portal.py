from __future__ import annotations

from collections import Counter, defaultdict
import json
import mimetypes
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today
from frappe.utils.file_manager import save_file

from qas_custom.services.attendance import update_attendance_status
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, get_attendance_entries


SPECIAL_ENROLLMENT_TYPES = {"Trial", "Makeup", "Pay-as-you-go"}
TEACHER_BLOCKED_ATTENDANCE_STATUSES = {"Cancelled"}
MAX_PHOTO_UPLOADS = 12
PHOTO_POST_PREVIEW_LIMIT = 6
MAX_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024
VALID_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")
VALID_VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm"}


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
    for row in sorted(attendance_rows, key=lambda item: item.get("creation") or ""):
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
                "source_doctype": row.get("source_doctype"),
                "source_document": row.get("source_document"),
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
        "photo_posts": _get_photo_post_rows(session["name"]),
        "video_posts": _get_video_post_rows(session["name"]),
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

    for update in updates:
        row_id = update.get("row_id")
        if not row_id:
            frappe.throw(_("Invalid attendance row."))
        if _is_blocked_teacher_attendance_update(session["name"], row_id, update):
            continue
        update_attendance_status(
            course_session=session["name"],
            attendance_row=row_id,
            status=update.get("status"),
            actor=frappe.session.user,
            comment=update.get("comments"),
        )

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


def publish_teacher_photo_post_data(course_session=None, title=None, caption=None):
    teacher = _require_teacher()
    form_payload = _get_request_form()
    course_session = course_session or form_payload.get("course_session")
    title = title if title is not None else form_payload.get("title")
    caption = caption if caption is not None else form_payload.get("caption")

    if not course_session:
        frappe.throw(_("Course session is required."))

    uploads = _get_uploaded_photos()
    if not uploads:
        frappe.throw(_("At least one photo is required."))
    if len(uploads) > MAX_PHOTO_UPLOADS:
        frappe.throw(_("Please upload {0} photos or fewer at a time.").format(MAX_PHOTO_UPLOADS))

    session = _get_owned_session(course_session, teacher.name)
    photo_post = frappe.get_doc(
        {
            "doctype": "Session Photo Post",
            "course_session": session["name"],
            "title": (title or "").strip() or "Class Photos",
            "caption": (caption or "").strip(),
            "status": "Draft",
            "teacher": teacher.name,
        }
    )
    photo_post.insert(ignore_permissions=True)

    for upload in uploads:
        _validate_uploaded_photo(upload)
        file_doc = save_file(
            _get_upload_filename(upload),
            _read_uploaded_file(upload),
            "Session Photo Post",
            photo_post.name,
            is_private=1,
        )
        photo_post.append("photos", {"image": file_doc.file_url})

    photo_post.status = "Published"
    photo_post.posted_at = now_datetime()
    photo_post.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "photo_post": _build_photo_post_payload(
            photo_post.name,
            photo_post.title,
            photo_post.caption,
            photo_post.status,
            photo_post.posted_at,
            len(photo_post.photos or []),
            _build_photo_previews_from_doc(photo_post),
        )
    }


def get_teacher_photo_content_data(photo_post=None, photo_idx=None):
    teacher = _require_teacher()
    if not photo_post:
        frappe.throw(_("Photo post is required."))

    photo_post_doc = frappe.get_doc("Session Photo Post", photo_post)
    _get_owned_session(photo_post_doc.get("course_session"), teacher.name)

    target_idx = cint(photo_idx)
    if target_idx <= 0:
        raise frappe.PermissionError

    photo_row = next((row for row in photo_post_doc.photos or [] if cint(row.idx) == target_idx), None)
    if not photo_row or not getattr(photo_row, "image", None):
        raise frappe.DoesNotExistError

    file_doc_name = frappe.db.get_value("File", {"file_url": photo_row.image}, "name")
    if not file_doc_name:
        raise frappe.DoesNotExistError

    file_doc = frappe.get_doc("File", file_doc_name)
    content = file_doc.get_content()
    filename = file_doc.file_name or photo_row.image.rsplit("/", 1)[-1]
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return {
        "filename": filename,
        "content": content,
        "content_type": content_type,
    }


def publish_teacher_video_post_data(course_session=None, title=None, caption=None):
    teacher = _require_teacher()
    form_payload = _get_request_form()
    course_session = course_session or form_payload.get("course_session")
    title = title if title is not None else form_payload.get("title")
    caption = caption if caption is not None else form_payload.get("caption")

    if not course_session:
        frappe.throw(_("Course session is required."))

    upload = _get_uploaded_video()
    if not upload:
        frappe.throw(_("A video file is required."))

    _validate_uploaded_video(upload)
    content = _read_uploaded_file(upload, "video")
    if len(content) > MAX_VIDEO_UPLOAD_BYTES:
        frappe.throw(_("Please upload a video smaller than 100 MB."))

    session = _get_owned_session(course_session, teacher.name)
    video_post = frappe.get_doc(
        {
            "doctype": "Session Video Post",
            "course_session": session["name"],
            "title": (title or "").strip() or "Class Video",
            "caption": (caption or "").strip(),
            "status": "Draft",
            "teacher": teacher.name,
        }
    )
    video_post.insert(ignore_permissions=True)

    file_doc = save_file(
        _get_upload_filename(upload, "class-video.mp4"),
        content,
        "Session Video Post",
        video_post.name,
        is_private=1,
    )

    video_post.video = file_doc.file_url
    video_post.file_name = file_doc.file_name or _get_upload_filename(upload, "class-video.mp4")
    video_post.file_size = len(content)
    video_post.mime_type = (
        getattr(upload, "mimetype", None)
        or getattr(upload, "content_type", None)
        or ""
    )
    video_post.status = "Published"
    video_post.posted_at = now_datetime()
    video_post.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "video_post": _build_video_post_payload(
            video_post.name,
            video_post.title,
            video_post.caption,
            video_post.status,
            video_post.posted_at,
            video_post.file_name,
            video_post.file_size,
        )
    }


def get_teacher_video_content_data(video_post=None, download=False):
    teacher = _require_teacher()
    if not video_post:
        frappe.throw(_("Video post is required."))

    video_post_doc = frappe.get_doc("Session Video Post", video_post)
    _get_owned_session(video_post_doc.get("course_session"), teacher.name)

    if not video_post_doc.video:
        raise frappe.DoesNotExistError

    file_doc_name = frappe.db.get_value("File", {"file_url": video_post_doc.video}, "name")
    if not file_doc_name:
        raise frappe.DoesNotExistError

    file_doc = frappe.get_doc("File", file_doc_name)
    content = file_doc.get_content()
    filename = file_doc.file_name or video_post_doc.file_name or video_post_doc.video.rsplit("/", 1)[-1]
    content_type = video_post_doc.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return {
        "filename": filename,
        "content": content,
        "content_type": content_type,
        "display_content_as": "attachment" if cint(download) else "inline",
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

    fields = [
        "name",
        "course_session",
        "student",
        "enrollment_type",
        "status",
        "comments",
        "makeup_voucher",
        "source_doctype",
        "source_document",
        "marked_by",
        "marked_at",
        "previous_status",
        "creation",
    ]
    return get_attendance_entries(session_ids, fields=fields)


def _get_attendance_by_session(session_ids: list[str]):
    grouped = defaultdict(list)
    for row in _get_attendance_rows(session_ids):
        grouped[row.get("course_session")].append(row)
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


def _get_photo_post_rows(course_session: str):
    rows = frappe.get_all(
        "Session Photo Post",
        filters={"course_session": course_session},
        fields=["name", "title", "caption", "status", "posted_at"],
        order_by="posted_at desc, creation desc",
    )
    if not rows:
        return []

    photo_post_ids = [row.name for row in rows]
    photo_counts = _get_photo_counts(photo_post_ids)
    photo_previews = _get_photo_previews(photo_post_ids)
    return [
        _build_photo_post_payload(
            row.name,
            row.title,
            row.caption,
            row.status,
            row.posted_at,
            photo_counts.get(row.name, 0),
            photo_previews.get(row.name, []),
        )
        for row in rows
    ]


def _get_photo_counts(photo_post_ids: list[str]):
    if not photo_post_ids:
        return {}

    counts = defaultdict(int)
    for row in frappe.get_all(
        "Session Photo Item",
        filters={
            "parent": ["in", photo_post_ids],
            "parenttype": "Session Photo Post",
            "parentfield": "photos",
        },
        fields=["parent"],
    ):
        counts[row.parent] += 1
    return counts


def _get_photo_previews(photo_post_ids: list[str]):
    previews = defaultdict(list)
    for row in frappe.get_all(
        "Session Photo Item",
        filters={
            "parent": ["in", photo_post_ids],
            "parenttype": "Session Photo Post",
            "parentfield": "photos",
        },
        fields=["parent", "idx"],
        order_by="parent asc, idx asc",
    ):
        if len(previews[row.parent]) >= PHOTO_POST_PREVIEW_LIMIT:
            continue
        previews[row.parent].append(_build_photo_preview_payload(row.parent, row.idx))
    return previews


def _build_photo_previews_from_doc(photo_post):
    return [
        _build_photo_preview_payload(photo_post.name, row.idx)
        for row in (photo_post.photos or [])[:PHOTO_POST_PREVIEW_LIMIT]
    ]


def _build_photo_preview_payload(photo_post_id, photo_idx):
    return {
        "idx": cint(photo_idx),
        "preview_url": _build_teacher_photo_url(photo_post_id, photo_idx),
    }


def _build_photo_post_payload(photo_post_id, title, caption, status, posted_at, photo_count, photos=None):
    photos = photos or []
    return {
        "id": photo_post_id,
        "title": title or "Class Photos",
        "caption": caption or "",
        "status": status,
        "posted_at": _as_string(posted_at),
        "photo_count": photo_count,
        "photos": photos,
        "remaining_photo_count": max(cint(photo_count) - len(photos), 0),
    }


def _build_teacher_photo_url(photo_post_id, photo_idx):
    return (
        "/api/method/qas_custom.api.teacher_portal.teacher_portal_get_photo?"
        + urlencode({"photo_post": photo_post_id, "photo_idx": cint(photo_idx)})
    )


def _get_video_post_rows(course_session: str):
    return [
        _build_video_post_payload(
            row.name,
            row.title,
            row.caption,
            row.status,
            row.posted_at,
            row.file_name,
            row.file_size,
        )
        for row in frappe.get_all(
            "Session Video Post",
            filters={"course_session": course_session},
            fields=["name", "title", "caption", "status", "posted_at", "file_name", "file_size"],
            order_by="posted_at desc, creation desc",
        )
    ]


def _build_video_post_payload(video_post_id, title, caption, status, posted_at, file_name=None, file_size=None):
    preview_url = _build_teacher_video_url(video_post_id)
    return {
        "id": video_post_id,
        "title": title or "Class Video",
        "caption": caption or "",
        "status": status,
        "posted_at": _as_string(posted_at),
        "file_name": file_name or "",
        "file_size": file_size or 0,
        "preview_url": preview_url,
        "download_url": _build_teacher_video_url(video_post_id, download=True),
    }


def _build_teacher_video_url(video_post_id, download=False):
    query = {"video_post": video_post_id}
    if download:
        query["download"] = 1
    return (
        "/api/method/qas_custom.api.teacher_portal.teacher_portal_get_video?"
        + urlencode(query)
    )


def _count_special_students(attendance_rows: list[dict]):
    counter = Counter(row.get("enrollment_type") for row in attendance_rows)
    return {
        "trial": counter.get("Trial", 0),
        "makeup": counter.get("Makeup", 0),
        "pay_as_you_go": counter.get("Pay-as-you-go", 0),
    }


def _get_attendance_status_options():
    field = frappe.get_meta(ATTENDANCE_DOCTYPE).get_field("status")
    if not field or not field.options:
        return []

    return [
        option.strip()
        for option in field.options.splitlines()
        if option.strip() and option.strip() not in TEACHER_BLOCKED_ATTENDANCE_STATUSES
    ]


def _is_blocked_teacher_attendance_update(course_session, row_id, update):
    status = (update.get("status") or "").strip()
    if status not in TEACHER_BLOCKED_ATTENDANCE_STATUSES:
        return False

    current = frappe.db.get_value(
        ATTENDANCE_DOCTYPE,
        {"name": row_id, "course_session": course_session},
        ["status", "comments"],
        as_dict=True,
    )
    if current and current.status == status and (current.comments or "") == (update.get("comments") or ""):
        return True

    frappe.throw(_("Teachers cannot mark attendance as {0}.").format(status))


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


def _get_request_form():
    request = getattr(frappe.local, "request", None)
    form = getattr(request, "form", None) if request else None
    if not form:
        return {}
    return dict(form)


def _get_uploaded_photos():
    request = getattr(frappe.local, "request", None)
    files = getattr(request, "files", None) if request else None
    if not files:
        return []

    uploads = []
    if hasattr(files, "getlist"):
        uploads.extend(files.getlist("photos"))
        uploads.extend(files.getlist("photo"))

    if not uploads:
        uploads.extend(list(files.values()))

    return [upload for upload in uploads if upload and _get_upload_filename(upload)]


def _get_uploaded_video():
    request = getattr(frappe.local, "request", None)
    files = getattr(request, "files", None) if request else None
    if not files:
        return None

    if hasattr(files, "getlist"):
        for fieldname in ("video", "videos"):
            uploads = [upload for upload in files.getlist(fieldname) if upload]
            if uploads:
                return uploads[0]

    for upload in files.values():
        if upload:
            return upload

    return None


def _validate_uploaded_photo(upload):
    mimetype = (getattr(upload, "mimetype", None) or getattr(upload, "content_type", None) or "").lower()
    filename = _get_upload_filename(upload).lower()
    valid_extension = filename.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"))
    if not mimetype.startswith("image/") and not valid_extension:
        frappe.throw(_("Only image files can be uploaded."))


def _validate_uploaded_video(upload):
    mimetype = (getattr(upload, "mimetype", None) or getattr(upload, "content_type", None) or "").lower()
    filename = _get_upload_filename(upload, "class-video.mp4").lower()
    valid_extension = filename.endswith(VALID_VIDEO_EXTENSIONS)
    valid_mimetype = mimetype in VALID_VIDEO_MIME_TYPES or mimetype.startswith("video/")
    if not valid_mimetype and not valid_extension:
        frappe.throw(_("Only MP4, MOV, or WebM video files can be uploaded."))


def _get_upload_filename(upload, fallback="class-photo.jpg"):
    return (getattr(upload, "filename", None) or fallback).strip()


def _read_uploaded_file(upload, label="photo"):
    if hasattr(upload, "stream") and upload.stream:
        content = upload.stream.read()
    elif hasattr(upload, "read"):
        content = upload.read()
    else:
        frappe.throw(_("Could not read uploaded {0}.").format(label))

    if not content:
        frappe.throw(_("Uploaded {0} is empty.").format(label))
    return content


def _as_string(value):
    if value is None:
        return None
    return str(value)
