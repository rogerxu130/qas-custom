"""Term-scoped private media export and explicit, snapshot-bound cleanup."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import frappe
from frappe.utils import add_days, cint, get_datetime, now_datetime, today

from qas_custom.services.term_media_files import cleanup_eligibility, hash_file, segment, write_parts

ARCHIVE = "Term Media Archive"
REMOVAL = "Term Media Removal"
MEDIA = {"photo": "Session Photo Post", "video": "Session Video Post"}


class MediaArchivedError(frappe.DoesNotExistError):
    http_status_code = 410


def require_admin(write=False):
    from qas_custom.services.school_admin import _require_school_admin
    from qas_custom.services.support_view import reject_support_view_write
    _require_school_admin()
    # These archives span all classrooms, and are never a support-view capability.
    reject_support_view_write()


def _json(doc):
    return json.loads(doc.job_data or "{}")


def _update(name, data=None, **values):
    if data is not None:
        values["job_data"] = json.dumps(data, default=str, ensure_ascii=False)
    frappe.db.set_value(ARCHIVE, name, values, update_modified=True)


def _lock(doctype, name):
    # Names are bound values; callers only use internal constant doctypes.
    if not frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (name,)):
        frappe.throw("Record not found.", frappe.DoesNotExistError)


def _term(term, lock=False):
    if lock:
        _lock("Term", term)
    return frappe.db.get_value("Term", term, ["name", "status", "end_date"], as_dict=True)


def _eligible(term):
    row = _term(term)
    if not row:
        frappe.throw("Term not found.", frappe.DoesNotExistError)
    return cleanup_eligibility(row, today())


def _assert_eligible(term):
    allowed, reason = _eligible(term)
    if not allowed:
        frappe.throw(reason)


def validate_term_change(doc, method=None):
    """Block reopening/date changes while deletion is queued/running. Row lock closes check/use races."""
    if doc.is_new() or not frappe.db.exists("DocType", ARCHIVE):
        return
    _lock("Term", doc.name)
    old = frappe.db.get_value("Term", doc.name, ["status", "end_date"], as_dict=True)
    changed = old and (old.status != doc.status or str(old.end_date) != str(doc.end_date))
    if changed and frappe.db.exists(ARCHIVE, {"term": doc.name, "cleanup_status": ["in", ["Queued", "Running"]]}):
        frappe.throw("Media cleanup is in progress. Retry changing the term after it finishes.")


def _type(value):
    if value not in {"all", "photo", "video"}:
        frappe.throw("Invalid media type.")
    return value


def _all(doctype, filters=None, fields=None):
    offset = 0
    while True:
        rows = frappe.get_all(doctype, filters=filters or {}, fields=fields or ["name"],
                              order_by="name asc", limit_start=offset, limit_page_length=300)
        yield from rows
        if len(rows) < 300:
            break
        offset += len(rows)


def _key(kind, post, idx):
    return hashlib.sha256(f"{kind}\0{post}\0{idx}".encode()).hexdigest()[:32]


def _inventory(term, media_type="all"):
    entries, notes = [], {}
    stats = dict(sessions=0, photos=0, videos=0, bytes=0, legacy_photos=0, drafts=0)
    courses, files = {}, {}
    for slot in _all("Weekly Timeslot", {"term": term}, ["name", "course", "campus", "day_of_week", "start_time"]):
        course = slot.course or "Unassigned"
        if course not in courses:
            courses[course] = frappe.db.get_value("Course", course, "course_name") or course
        label = f"{slot.day_of_week or ''} {slot.start_time or ''} {slot.campus or ''}"
        for session in _all("Course Sessions", {"weekly_timeslot": slot.name}, ["name", "session_date", "photos"]):
            stats["sessions"] += 1
            stats["legacy_photos"] += bool(session.get("photos"))
            base = dict(term=term, course=course, course_label=courses[course], campus=slot.campus or "",
                        timeslot=slot.name, timeslot_label=label, session=session.name, session_date=str(session.session_date))
            text = []
            for homework in _all("Session Homework", {"course_session": session.name, "status": "Published"},
                                 ["name", "title", "description", "teacher", "published_at"]):
                text.append(f"{homework.title}\n{homework.teacher or ''} | {homework.published_at}\n{homework.description or ''}")
            for kind, doctype in MEDIA.items():
                stats["drafts"] += frappe.db.count(doctype, {"course_session": session.name, "status": "Draft"})
                for post in _all(doctype, {"course_session": session.name, "status": "Published"}, ["*"]):
                    text.append(f"{post.title}\n{post.teacher or ''} | {post.posted_at}\n{post.caption or ''}")
                    media_rows = (list(_all("Session Photo Item", {"parent": post.name, "parenttype": doctype, "parentfield": "photos"},
                                           ["name", "idx", "image"])) if kind == "photo" else
                                  [frappe._dict(name=post.name, idx=0, image=post.video)])
                    for media in media_rows:
                        if not media.image:
                            continue
                        stats["photos" if kind == "photo" else "videos"] += 1
                        if media.image not in files:
                            files[media.image] = frappe.db.get_value("File", {"file_url": media.image},
                                ["name", "file_name", "file_size", "modified", "content_hash"], as_dict=True)
                        file = files[media.image]
                        if media_type not in {"all", kind}:
                            continue
                        key = _key(kind, post.name, cint(media.idx))
                        entries.append(dict(base, key=key, kind=kind, post=post.name, idx=cint(media.idx), row=media.name,
                                            teacher=post.teacher or "", published_at=str(post.posted_at or ""),
                                            url=media.image, file_id=file.name if file else "", filename=file.file_name if file else "missing",
                                            file_modified=str(file.modified) if file else "", content_hash=file.content_hash if file else "",
                                            bytes=cint(file.file_size) if file else 0, status="pending", reason=""))
            path = "/".join([segment(term), segment(courses[course], course), segment(label, slot.name),
                             segment(f'{session.session_date} {session.name}', session.name), "class-notes.txt"])
            notes[path] = "\n\n".join(text) or "No published classroom notes."
    stats["bytes"] = sum(cint(file.file_size) for file in {file.name: file for file in files.values() if file}.values())
    stats["courses"] = len(courses)
    stats["unique_files"] = len({file.name for file in files.values() if file})
    return entries, notes, stats


def _directory(name):
    # Never use a user-provided filename as a path component.
    return Path(frappe.get_site_path("private", "term-media", hashlib.sha256(name.encode()).hexdigest()))


def _local_path(file):
    original = Path(file.get_full_path())
    if original.is_symlink():
        raise ValueError("unsupported_storage")
    path = original.resolve()
    roots = [Path(frappe.get_site_path("private", "files")).resolve(), Path(frappe.get_site_path("public", "files")).resolve()]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("unsupported_storage")
    return path


def _source(row, check_hash=False):
    """Return an unchanged local source. All membership and identity checks are server-side."""
    if not row.get("file_id") or not frappe.db.exists("File", row["file_id"]):
        raise ValueError("file_missing")
    file = frappe.get_doc("File", row["file_id"])
    if file.file_url != row["url"] or str(file.modified) != row["file_modified"]:
        raise ValueError("file_changed")
    if file.attached_to_doctype != MEDIA[row["kind"]] or file.attached_to_name != row["post"]:
        raise ValueError("file_attachment_changed")
    post = frappe.db.get_value(MEDIA[row["kind"]], row["post"], ["course_session", "status"], as_dict=True)
    if not post or post.course_session != row["session"] or post.status != "Published":
        raise ValueError("membership_changed")
    slot = frappe.db.get_value("Course Sessions", row["session"], "weekly_timeslot")
    if slot != row["timeslot"] or frappe.db.get_value("Weekly Timeslot", slot, "term") != row["term"]:
        raise ValueError("membership_changed")
    if row["kind"] == "photo":
        link = frappe.db.get_value("Session Photo Item", row["row"], ["parent", "idx", "image"], as_dict=True)
        if not link or link.parent != row["post"] or cint(link.idx) != row["idx"] or link.image != row["url"]:
            raise ValueError("file_changed")
    elif frappe.db.get_value("Session Video Post", row["post"], "video") != row["url"]:
        raise ValueError("file_changed")
    path = _local_path(file)
    if check_hash and hash_file(path) != (row["sha256"], row["bytes"]):
        raise ValueError("file_changed")
    return path


def payload(doc):
    data = _json(doc)
    return dict(name=doc.name, term=doc.term, media_type=doc.media_type, status=doc.status,
                progress=cint(doc.progress), total=cint(doc.total), expires_at=str(doc.expires_at or ""),
                backup_confirmed_at=str(doc.backup_confirmed_at or ""), parts=data.get("parts", []),
                cleanup_status=doc.cleanup_status or "", cleanup_results=data.get("cleanup_results", []),
                summary=data.get("summary", {}), error=doc.error or "")


def overview(term):
    require_admin()
    eligible, reason = _eligible(term)
    _, _, stats = _inventory(term)
    archives = [payload(frappe.get_doc(ARCHIVE, row.name)) for row in frappe.get_all(ARCHIVE,
                filters={"term": term}, fields=["name"], order_by="creation desc", limit_page_length=30)]
    return dict(term=_term(term), eligible=eligible, reason=reason, stats=stats, archives=archives)


def start_archive(term, media_type="all"):
    require_admin(write=True)
    _type(media_type)
    _term(term, lock=True)
    active = frappe.db.get_value(ARCHIVE, {"term": term, "status": ["in", ["Queued", "Running"]]}, "name")
    if active:
        return payload(frappe.get_doc(ARCHIVE, active))
    doc = frappe.get_doc(dict(doctype=ARCHIVE, term=term, media_type=media_type, status="Queued", job_data="{}"))
    doc.insert(ignore_permissions=True)
    # A durable exact enumeration is captured before the worker starts. No later upload is added.
    entries, notes, stats = _inventory(term, media_type)
    _update(doc.name, dict(entries=entries, notes=notes, stats=stats, parts=[], cleanup_results=[]), total=len(entries))
    frappe.enqueue("qas_custom.services.term_media.build_archive", queue="long", timeout=7200,
                   enqueue_after_commit=True, archive=doc.name)
    return payload(frappe.get_doc(ARCHIVE, doc.name))


def get_archive(archive):
    require_admin()
    return payload(frappe.get_doc(ARCHIVE, archive))


def build_archive(archive):
    _lock(ARCHIVE, archive)
    doc = frappe.get_doc(ARCHIVE, archive)
    if doc.status != "Queued":
        frappe.db.rollback()
        return
    _update(archive, status="Running")
    frappe.db.commit()
    data = _json(doc)
    try:
        require_admin(write=True)
        def progress(count, rows, parts):
            # Progress heartbeat; unpublished packages never gain cleanup eligibility.
            if count % 10 == 0 or count == len(rows):
                _update(archive, progress=count)
                frappe.db.commit()
        parts = write_parts(_directory(archive), data["entries"], _source, progress=progress, notes=data.get("notes"))
        data["parts"] = parts
        exported = sum(row["status"] == "exported" for row in data["entries"])
        failed = len(data["entries"]) - exported
        data["summary"] = dict(exported=exported, failed=failed)
        _update(archive, data, status="Partial" if failed else "Completed", progress=len(data["entries"]),
                expires_at=add_days(now_datetime(), 7), error="")
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        _update(archive, status="Failed", error="archive_failed")
        frappe.db.commit()
        frappe.log_error(title="Term media archive failed")


def confirm_backup(archive):
    require_admin(write=True)
    _lock(ARCHIVE, archive)
    doc = frappe.get_doc(ARCHIVE, archive)
    if doc.backup_confirmed_at:
        return payload(doc)
    if doc.status not in {"Completed", "Partial"} or not doc.expires_at or get_datetime(doc.expires_at) <= now_datetime():
        frappe.throw("archive_unavailable")
    data = _json(doc)
    if not data.get("parts") or any(not (_directory(archive) / part["filename"]).is_file() for part in data["parts"]):
        frappe.throw("archive_unavailable")
    _update(archive, backup_confirmed_at=now_datetime(), backup_confirmed_by=frappe.session.user)
    return payload(frappe.get_doc(ARCHIVE, archive))


def _reference_fields():
    """Scan attachment and rich-text fields conservatively, including custom fields."""
    fields = list(frappe.get_all("DocField", filters={"fieldtype": ["in", ["Attach", "Attach Image", "Text Editor", "HTML Editor", "HTML"]]},
                                fields=["parent as dt", "fieldname", "fieldtype"], limit_page_length=0))
    fields += list(frappe.get_all("Custom Field", filters={"fieldtype": ["in", ["Attach", "Attach Image", "Text Editor", "HTML Editor", "HTML"]]},
                                 fields=["dt", "fieldname", "fieldtype"], limit_page_length=0))
    return {(r.dt, r.fieldname, r.fieldtype) for r in fields}


def _shared(row, reference_fields):
    for file in frappe.get_all("File", filters={"file_url": row["url"]}, fields=["name"], limit_page_length=0):
        if file.name != row["file_id"]:
            return True
    if frappe.db.exists("File", {"thumbnail_url": row["url"]}):
        return True
    if row.get("content_hash") and frappe.db.exists("File", {"content_hash": row["content_hash"], "name": ["!=", row["file_id"]]}):
        return True
    own = ("Session Photo Item", "image", row["row"]) if row["kind"] == "photo" else ("Session Video Post", "video", row["post"])
    for dt, field, fieldtype in reference_fields:
        if dt in {"File", ARCHIVE, REMOVAL}:
            continue
        meta = frappe.get_meta(dt)
        if meta.issingle:
            value = frappe.db.get_single_value(dt, field)
            if value and row["url"] in str(value):
                return True
            continue
        if meta.is_virtual or not frappe.db.has_column(dt, field):
            continue
        filters = {field: row["url"] if fieldtype in {"Attach", "Attach Image"} else ["like", "%" + row["url"] + "%"]}
        if (dt, field) == own[:2]:
            filters["name"] = ["!=", own[2]]
        if dt == "Comment":
            comments = frappe.get_all(dt, filters=filters, fields=["comment_type", "reference_doctype", "reference_name"], limit_page_length=0)
            if any(not (comment.comment_type in {"Attachment", "Attachment Removed"}
                        and comment.reference_doctype == MEDIA[row["kind"]]
                        and comment.reference_name == row["post"]) for comment in comments):
                return True
        elif frappe.db.exists(dt, filters):
            return True
    return False


def _candidate(row, reference_fields, check_hash=True):
    result = dict(key=row["key"], post=row["post"], kind=row["kind"], idx=row["idx"], bytes=row["bytes"], status="skipped", reason="not_archived")
    removed = frappe.db.get_value(REMOVAL, row["key"], "state")
    if removed == "Removed":
        result["reason"] = "already_removed"
        return result
    if row["status"] != "exported":
        return result
    try:
        _source(row, check_hash=check_hash)
        if _shared(row, reference_fields):
            result["reason"] = "shared_file"
        else:
            result.update(status="ready", reason="")
    except (OSError, ValueError):
        result["reason"] = "file_missing_or_changed"
    return result


def preview_cleanup(archive, media_type="all"):
    require_admin()
    _type(media_type)
    doc = frappe.get_doc(ARCHIVE, archive)
    allowed, reason = _eligible(doc.term)
    if not doc.backup_confirmed_at:
        allowed, reason = False, "backup_not_confirmed"
    data = _json(doc)
    items = []
    if allowed:
        reference_fields = _reference_fields()
        for row in data.get("entries", []):
            if media_type in {"all", row["kind"]}:
                items.append(_candidate(row, reference_fields, check_hash=False))
    return dict(eligible=allowed, reason=reason, items=items, counts=dict(
        ready=sum(r["status"] == "ready" for r in items), skipped=sum(r["status"] != "ready" for r in items),
        bytes=sum(r["bytes"] for r in items if r["status"] == "ready")))


def start_cleanup(archive, media_type="all", confirmation=""):
    require_admin(write=True)
    _type(media_type)
    doc = frappe.get_doc(ARCHIVE, archive)
    # Lock order is always Term then Archive, shared with the worker and Term validation.
    _term(doc.term, lock=True)
    _lock(ARCHIVE, archive)
    doc.reload()
    _assert_eligible(doc.term)
    if confirmation != doc.term:
        frappe.throw("term_confirmation_required")
    if not doc.backup_confirmed_at:
        frappe.throw("backup_not_confirmed")
    if doc.cleanup_status in {"Queued", "Running"}:
        return payload(doc)
    if frappe.db.exists(ARCHIVE, {"term": doc.term, "cleanup_status": ["in", ["Queued", "Running"]]}):
        frappe.throw("cleanup_already_running")
    data = _json(doc)
    # Each explicit run has a fixed selection. Retrying the same archive never includes fresh uploads.
    data["cleanup_keys"] = [r["key"] for r in data.get("entries", []) if r["status"] == "exported" and media_type in {"all", r["kind"]}]
    if not data["cleanup_keys"]:
        frappe.throw("no_archived_files")
    data["cleanup_actor"] = frappe.session.user
    data["cleanup_requested_at"] = str(now_datetime())
    data["cleanup_results"] = []
    _update(archive, data, cleanup_status="Queued", error="")
    frappe.enqueue("qas_custom.services.term_media.clean_archive", queue="long", timeout=7200,
                   enqueue_after_commit=True, archive=archive)
    return payload(frappe.get_doc(ARCHIVE, archive))


def _lock_source(row):
    # Freeze class membership and attachment while checking/deleting this file.
    _lock("Weekly Timeslot", row["timeslot"])
    _lock("Course Sessions", row["session"])
    _lock(MEDIA[row["kind"]], row["post"])
    if row["kind"] == "photo":
        _lock("Session Photo Item", row["row"])


def _source_link_matches(row):
    post = frappe.db.get_value(MEDIA[row["kind"]], row["post"], ["course_session", "status"], as_dict=True)
    if not post or post.course_session != row["session"] or post.status != "Published":
        return False
    if frappe.db.get_value("Course Sessions", row["session"], "weekly_timeslot") != row["timeslot"]:
        return False
    if frappe.db.get_value("Weekly Timeslot", row["timeslot"], "term") != row["term"]:
        return False
    if row["kind"] == "photo":
        link = frappe.db.get_value("Session Photo Item", row["row"], ["parent", "idx", "image"], as_dict=True)
        return bool(link and link.parent == row["post"] and cint(link.idx) == row["idx"] and link.image == row["url"])
    return frappe.db.get_value("Session Video Post", row["post"], "video") == row["url"]


def _clear_source(row):
    # Source records are locked by the caller, but never clear a replaced attachment during recovery.
    if not _source_link_matches(row):
        raise ValueError("membership_changed")
    if row["kind"] == "photo":
        frappe.db.set_value("Session Photo Item", row["row"], "image", None, update_modified=False)
    else:
        frappe.db.set_value("Session Video Post", row["post"], "video", None, update_modified=False)


def _remove_one(archive, row, actor, reference_fields):
    _term(row["term"], lock=True)
    _assert_eligible(row["term"])
    _lock_source(row)
    state = frappe.db.get_value(REMOVAL, row["key"], "state")
    if state == "Removed":
        return dict(key=row["key"], post=row["post"], kind=row["kind"], status="skipped", reason="already_removed", bytes=0)
    # Recovery after a process dies between physical unlink and database commit.
    if state == "Removing":
        if not _source_link_matches(row):
            return dict(key=row["key"], post=row["post"], kind=row["kind"], status="skipped", reason="file_missing_or_changed", bytes=0)
        file_exists = frappe.db.exists("File", row["file_id"])
        if not file_exists:
            _clear_source(row)
            frappe.db.set_value(REMOVAL, row["key"], {"state": "Removed", "removed_at": now_datetime()})
            return dict(key=row["key"], post=row["post"], kind=row["kind"], status="removed", reason="recovered", bytes=0)
        path = _local_path(frappe.get_doc("File", row["file_id"]))
        if not path.exists():
            # Frappe deletion tolerates an already absent physical file; verify original URL/ownership first.
            file = frappe.get_doc("File", row["file_id"])
            if file.file_url != row["url"] or file.attached_to_name != row["post"]:
                raise ValueError("file_changed")
            frappe.delete_doc("File", row["file_id"], ignore_permissions=True)
            _clear_source(row)
            frappe.db.set_value(REMOVAL, row["key"], {"state": "Removed", "removed_at": now_datetime()})
            return dict(key=row["key"], post=row["post"], kind=row["kind"], status="removed", reason="recovered", bytes=0)
    candidate = _candidate(row, reference_fields)
    if candidate["status"] != "ready":
        return candidate
    path = _source(row, check_hash=True)
    if state not in {"Removing", "Removed"}:
        doc = frappe.get_doc(dict(doctype=REMOVAL, archive=archive, term=row["term"], post=row["post"], kind=row["kind"],
                                  photo_idx=row["idx"], state="Removing", file_id=row["file_id"], file_url=row["url"], removed_by=actor))
        doc.name = row["key"]
        if state:
            frappe.db.set_value(REMOVAL, row["key"], {"state": "Removing", "archive": archive, "removed_by": actor})
        else:
            doc.insert(ignore_permissions=True, set_name=row["key"])
        # Durable intent before filesystem mutation. Reacquire locks and revalidate afterwards.
        frappe.db.commit()
        _term(row["term"], lock=True)
        _assert_eligible(row["term"])
        _lock_source(row)
    _lock(REMOVAL, row["key"])
    _lock("File", row["file_id"])
    _source(row, check_hash=True)
    if _shared(row, reference_fields):
        frappe.db.set_value(REMOVAL, row["key"], "state", "Cancelled")
        return dict(candidate, status="skipped", reason="shared_file", bytes=0)
    frappe.delete_doc("File", row["file_id"], ignore_permissions=True)
    if path.exists():
        raise OSError("file_not_released")
    _clear_source(row)
    frappe.db.set_value(REMOVAL, row["key"], {"state": "Removed", "removed_at": now_datetime()})
    return dict(candidate, status="removed", reason="", bytes=row["bytes"])


def clean_archive(archive):
    doc = frappe.get_doc(ARCHIVE, archive)
    _term(doc.term, lock=True)
    _lock(ARCHIVE, archive)
    doc.reload()
    if doc.cleanup_status != "Queued":
        frappe.db.rollback()
        return
    data = _json(doc)
    try:
        require_admin(write=True)
        _assert_eligible(doc.term)
        if not doc.backup_confirmed_at:
            frappe.throw("backup_not_confirmed")
        _update(archive, cleanup_status="Running")
        frappe.db.commit()
        reference_fields = _reference_fields()
        keys = set(data.get("cleanup_keys", []))
        for row in data.get("entries", []):
            if row["key"] not in keys:
                continue
            try:
                result = _remove_one(archive, row, data["cleanup_actor"], reference_fields)
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
                result = dict(key=row["key"], post=row["post"], kind=row["kind"], status="failed", reason="cleanup_failed", bytes=0)
                frappe.log_error(title="Term media file cleanup failed")
            data["cleanup_results"].append(result)
            _update(archive, data)
            frappe.db.commit()
            # Stop if an external/direct DB write changed the term despite the validation hook.
            _assert_eligible(doc.term)
        failed = any(r["status"] in {"failed", "skipped"} and r.get("reason") != "already_removed" for r in data["cleanup_results"])
        _update(archive, data, cleanup_status="Partial" if failed else "Completed")
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        _update(archive, data, cleanup_status="Failed", error="cleanup_failed")
        frappe.db.commit()
        frappe.log_error(title="Term media cleanup stopped")


def download_path(archive, part):
    require_admin()
    doc = frappe.get_doc(ARCHIVE, archive)
    if doc.status not in {"Completed", "Partial"} or not doc.expires_at or get_datetime(doc.expires_at) <= now_datetime():
        frappe.throw("archive_unavailable")
    item = next((row for row in _json(doc).get("parts", []) if row["index"] == cint(part)), None)
    if not item:
        frappe.throw("Archive part not found.", frappe.DoesNotExistError)
    path = _directory(archive) / f'part-{cint(part):03d}.zip'
    if not path.is_file():
        frappe.throw("archive_unavailable")
    return path


def expire_archives():
    """Remove temporary packages, keep audit snapshots and cleanup evidence."""
    for row in list(_all(ARCHIVE, {"expires_at": ["<", now_datetime()], "status": ["in", ["Completed", "Partial"]]}, ["name"])):
        _lock(ARCHIVE, row.name)
        shutil.rmtree(_directory(row.name), ignore_errors=False) if _directory(row.name).exists() else None
        _update(row.name, status="Expired")
        frappe.db.commit()
    # Jobs which outlive their two-hour execution window are not left permanently pending.
    for row in _all(ARCHIVE, {"modified": ["<", add_days(now_datetime(), -1)]}, ["name", "status", "cleanup_status"]):
        if row.status in {"Queued", "Running"}:
            shutil.rmtree(_directory(row.name), ignore_errors=True)
            _update(row.name, status="Failed", error="worker_interrupted")
        if row.cleanup_status in {"Queued", "Running"}:
            _update(row.name, cleanup_status="Failed", error="worker_interrupted")
        frappe.db.commit()


def assert_media_available(post, kind, idx=None):
    if not frappe.db.exists("DocType", REMOVAL):
        return
    if frappe.db.get_value(REMOVAL, _key(kind, post, cint(idx)), "state") == "Removed":
        frappe.local.response.http_status_code = 410
        frappe.throw("Media archived and removed.", MediaArchivedError)


def annotate_media(items):
    if not items or not frappe.db.exists("DocType", REMOVAL):
        return items
    ids = [item.get("id") for item in items if item.get("id")]
    if not ids:
        return items
    removed = {}
    for row in frappe.get_all(REMOVAL, filters={"post": ["in", ids], "state": "Removed"},
                              fields=["post", "kind", "photo_idx"], limit_page_length=0):
        removed.setdefault((row.kind, row.post), set()).add(cint(row.photo_idx))
    for item in items:
        kind = {"photo_post": "photo", "video_post": "video"}.get(item.get("type"))
        indices = removed.get((kind, item.get("id")), set())
        if not indices:
            continue
        item["media_archived_count"] = len(indices)
        if kind == "video":
            item["media_archived"] = True
            for key in ("video_url", "preview_url", "download_url"):
                item[key] = ""
            item["video"] = None
        else:
            original_photos = item.get("photos") or []
            parent_shape = "students" in item or "course_session" in item
            if parent_shape:
                # Parent feed already excludes child rows with empty image URLs.
                photos = original_photos
                count = len(photos)
            else:
                photos = [photo for photo in original_photos if cint(photo.get("idx")) not in indices]
                count = max(0, cint(item.get("photo_count", len(original_photos))) - len(indices))
            item["photos"] = photos
            item["photo_count"] = count
            item["remaining_photo_count"] = max(0, count - len(photos))
            item["media_archived"] = count == 0
            item["cover_image"] = photos[0] if parent_shape and photos else None
    return items



def guard_media_reference(doc, method=None):
    """Do not attach files currently being removed or already removed elsewhere."""
    if doc.doctype in {ARCHIVE, REMOVAL} or not frappe.db.exists("DocType", REMOVAL):
        return
    # Removed slots are immutable: new uploads must be new posts, not replacements hidden by old tombstones.
    slots = []
    if doc.doctype == "Session Video Post" and doc.get("video"):
        slots.append(_key("video", doc.name, 0))
    elif doc.doctype == "Session Photo Post":
        slots.extend(_key("photo", doc.name, cint(row.idx)) for row in doc.get("photos", []) if row.get("image"))
    elif doc.doctype == "Session Photo Item" and doc.get("image"):
        slots.append(_key("photo", doc.parent, cint(doc.idx)))
    for key in slots:
        if frappe.db.exists(REMOVAL, {"name": key, "state": ["in", ["Removing", "Removed"]]}):
            _lock(REMOVAL, key)
            frappe.throw("Archived media slots cannot be replaced. Create a new media post.")
    values = []
    if doc.doctype == "File":
        values.append(doc.get("file_url"))
    for record in [doc, *doc.get_all_children()]:
        for field in record.meta.fields:
            if field.fieldtype in {"Attach", "Attach Image", "Text Editor", "HTML Editor", "HTML"}:
                values.append(record.get(field.fieldname))
    values = [str(value) for value in values if value and "/files/" in str(value)]
    if not values:
        return
    for removal in _all(REMOVAL, {"state": ["in", ["Removing", "Removed"]]}, ["name", "file_url"]):
        if removal.file_url and any(removal.file_url in value for value in values):
            _lock(REMOVAL, removal.name)
            frappe.throw("This media has been archived for removal. Upload a new file instead.")
