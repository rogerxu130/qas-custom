from __future__ import annotations

import imghdr
import json
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, sanitize_html
from frappe.utils.file_manager import save_file

from qas_custom.services.teacher_portal import _require_teacher


ARTICLE_DOCTYPE = "Teacher Training Article"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_FORMATS = {"jpeg": "jpg", "png": "png", "webp": "webp"}
ADMIN_ROLES = {"School Admin", "System Manager"}
STARTER_TITLE = "NDIS common situations and supporting students"


def ensure_teacher_training_starter_article():
    """Create a safe, editable first guide once the Training DocType exists."""
    if not frappe.db.exists("DocType", ARTICLE_DOCTYPE):
        return
    if frappe.db.exists(ARTICLE_DOCTYPE, {"title": STARTER_TITLE}):
        return
    doc = frappe.get_doc(
        {
            "doctype": ARTICLE_DOCTYPE,
            "title": STARTER_TITLE,
            "summary": "A practical starting guide for supporting NDIS students in class.",
            "status": "Published",
            "sort_order": 1,
            "content": """
<h2>Purpose</h2>
<p>This is practical classroom guidance, not clinical advice. Always follow the parent or guardian's instructions, the student's teaching notes, and Queensland Art School safeguarding procedures.</p>
<h2>Before class</h2>
<ul><li>Read the student's teaching notes and any agreed support strategies.</li><li>Prepare a calm workspace and simple, step-by-step instructions.</li><li>Ask the School Admin if anything is unclear before the class starts.</li></ul>
<h2>During class</h2>
<ul><li>Use clear language, visual examples, and one instruction at a time where helpful.</li><li>Offer a short break or a quieter option if the student becomes overwhelmed.</li><li>Do not make assumptions about a student's ability or diagnosis. Ask respectfully and follow the agreed plan.</li></ul>
<h2>If something changes</h2>
<p>For distress, illness, safety concerns, or a situation outside the agreed support plan, contact the Campus Admin or School Admin promptly and record factual observations only.</p>
<h2>After class</h2>
<p>Add relevant teaching observations through the usual class workflow. Keep language factual, respectful, and limited to what is useful for supporting the next lesson.</p>
""".strip(),
            "published_at": now_datetime(),
            "published_by": "Administrator",
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()


def get_school_admin_training_articles_data(status=None, limit=120):
    _require_school_admin()
    filters = {"status": status} if status else {}
    rows = frappe.get_all(
        ARTICLE_DOCTYPE,
        filters=filters,
        fields=["name", "title", "summary", "status", "sort_order", "published_at", "modified"],
        order_by="sort_order asc, modified desc",
        limit=_limit(limit, 120, 300),
    )
    return {"items": [_normalise_article(row, include_content=False) for row in rows]}


def get_school_admin_training_article_data(article=None):
    _require_school_admin()
    if not article:
        frappe.throw(_("Training article is required."))
    return _article_payload(frappe.get_doc(ARTICLE_DOCTYPE, article))


def save_school_admin_training_article_data(article=None, payload=None):
    _require_school_admin()
    data = _parse_payload(payload)
    doc = frappe.get_doc(ARTICLE_DOCTYPE, article) if article else frappe.new_doc(ARTICLE_DOCTYPE)
    _apply_article(doc, data)
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _article_payload(doc)


def publish_school_admin_training_article_data(article=None, payload=None):
    """Publish the current editor state without requiring a manual Draft save."""
    _require_school_admin()
    data = _parse_payload(payload) if payload is not None else None
    savepoint = "school_admin_publish_training_article"
    frappe.db.savepoint(savepoint)
    try:
        doc = _get_article(article) if article else frappe.new_doc(ARTICLE_DOCTYPE)
        if data is not None:
            _apply_article(doc, data)
        _validate_article(doc, require_content=True)
        doc.status = "Published"
        if not doc.published_at:
            doc.published_at = now_datetime()
            doc.published_by = frappe.session.user
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return _article_payload(doc)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def unpublish_school_admin_training_article_data(article=None):
    _require_school_admin()
    doc = _get_article(article)
    doc.status = "Draft"
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _article_payload(doc)


def delete_school_admin_training_article_data(article=None):
    _require_school_admin()
    doc = _get_article(article)
    frappe.delete_doc(ARTICLE_DOCTYPE, doc.name, force=True, ignore_permissions=True)
    frappe.db.commit()
    return {"deleted": doc.name}


def upload_school_admin_training_inline_image_data(article=None):
    _require_school_admin()
    doc = _get_article(article)
    upload = frappe.request.files.get("image") if frappe.request else None
    if not upload:
        frappe.throw(_("Choose an image to upload."))
    content = upload.read()
    if not content or len(content) > MAX_IMAGE_BYTES:
        frappe.throw(_("Images must be 5 MB or smaller."))
    image_type = imghdr.what(None, h=content)
    if image_type not in IMAGE_FORMATS:
        frappe.throw(_("Use a JPG, PNG, or WEBP image."))
    base_name = (upload.filename or "training-image").rsplit(".", 1)[0]
    file_doc = save_file(
        f"{base_name}.{IMAGE_FORMATS[image_type]}",
        content,
        ARTICLE_DOCTYPE,
        doc.name,
        is_private=0,
        df="inline_image",
    )
    frappe.db.commit()
    return {"image_url": file_doc.file_url, "file_name": file_doc.file_name}


def get_teacher_training_articles_data(limit=120):
    _require_teacher()
    rows = frappe.get_all(
        ARTICLE_DOCTYPE,
        filters={"status": "Published"},
        fields=["name", "title", "summary", "status", "sort_order", "published_at", "modified"],
        order_by="sort_order asc, published_at desc, modified desc",
        limit=_limit(limit, 120, 300),
    )
    return {"items": [_normalise_article(row, include_content=False) for row in rows]}


def get_teacher_training_article_data(article=None):
    _require_teacher()
    if not article:
        frappe.throw(_("Training article is required."))
    if not frappe.db.exists(ARTICLE_DOCTYPE, {"name": article, "status": "Published"}):
        frappe.throw(_("Training article not found."), frappe.DoesNotExistError)
    return _article_payload(frappe.get_doc(ARTICLE_DOCTYPE, article))


def _require_school_admin():
    if not (ADMIN_ROLES & set(frappe.get_roles(frappe.session.user))):
        frappe.throw(_("Only School Admin users can manage teacher training."), frappe.PermissionError)


def _get_article(article):
    if not article:
        frappe.throw(_("Training article is required."))
    return frappe.get_doc(ARTICLE_DOCTYPE, article)


def _parse_payload(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            frappe.throw(_("Training article payload is invalid."))
    if not isinstance(payload, dict):
        frappe.throw(_("Training article payload is invalid."))
    return payload


def _apply_article(doc, data):
    doc.title = str(data.get("title") or "").strip()
    doc.summary = str(data.get("summary") or "").strip()
    doc.content = sanitize_html(str(data.get("content") or "").strip())
    doc.sort_order = cint(data.get("sort_order") or 0)
    doc.links = []
    for index, row in enumerate(data.get("links") or []):
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        url = str(row.get("url") or "").strip()
        if not label and not url:
            continue
        if not label or not _is_safe_external_url(url):
            frappe.throw(_("Each training link needs a label and a valid http or https URL."))
        doc.append("links", {"label": label, "url": url, "sort_order": cint(row.get("sort_order") or index)})
    _validate_article(doc, require_content=False)


def _validate_article(doc, require_content=True):
    if not str(doc.title or "").strip():
        frappe.throw(_("Training title is required."))
    if require_content and not str(doc.content or "").strip():
        frappe.throw(_("Training content is required."))


def _is_safe_external_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _article_payload(doc):
    payload = _normalise_article(doc, include_content=True)
    payload["links"] = [
        {"label": row.label, "url": row.url, "sort_order": cint(row.sort_order or 0)}
        for row in sorted(doc.get("links") or [], key=lambda item: (cint(item.sort_order or 0), item.idx))
    ]
    return payload


def _normalise_article(row, include_content=False):
    payload = {
        "name": row.get("name"),
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "status": row.get("status") or "Draft",
        "sort_order": cint(row.get("sort_order") or 0),
        "published_at": row.get("published_at"),
        "modified": row.get("modified"),
    }
    if include_content:
        payload["content"] = row.get("content") or ""
    return payload


def _limit(value, default, max_value):
    return max(1, min(cint(value or default), max_value))
