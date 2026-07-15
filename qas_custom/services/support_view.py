from __future__ import annotations

import hashlib
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime


ADMIN_ROLES = {"School Admin"}
SUPPORT_TOKEN_HEADER = "X-QAS-Support-View-Token"
TARGETS = {
	"Parent": {"doctype": "Parent", "path": "/support-view"},
	"Teacher": {"doctype": "Teacher", "path": "/support-view"},
	"Campus Admin": {"doctype": "Campus Admin Profile", "path": "/campus-admin/support-view"},
}
DEFAULT_PARENT_PORTAL_URL = "https://portal.queenslandartschool.com"
DEFAULT_TEACHER_PORTAL_URL = "https://teacher.queenslandartschool.com"


def create_support_view_token(target_type=None, target=None, reason=None):
	_require_school_admin()
	target_type = _validate_target_type(target_type)
	if not target or not frappe.db.exists(TARGETS[target_type]["doctype"], target):
		frappe.throw(_("The selected support-view target no longer exists."))
	if target_type == "Teacher" and _has_field("Teacher", "status") and frappe.db.get_value("Teacher", target, "status") != "Active":
		frappe.throw(_("Only active teachers can be opened in support view."))
	if target_type == "Campus Admin" and not cint(frappe.db.get_value("Campus Admin Profile", target, "active")):
		frappe.throw(_("Only active Campus Admin profiles can be opened in support view."))

	raw_token = secrets.token_urlsafe(32)
	expires_at = add_to_date(now_datetime(), minutes=15)
	doc = frappe.get_doc(
		{
			"doctype": "Support View Token",
			"viewer_user": frappe.session.user,
			"target_type": target_type,
			"target_doctype": TARGETS[target_type]["doctype"],
			"target": target,
			"token_hash": _token_hash(raw_token),
			"reason": (reason or "").strip(),
			"expires_at": expires_at,
			"revoked": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	_log_event(doc, "Created")
	frappe.db.commit()
	return {
		"token": raw_token,
		"target_type": target_type,
		"target": target,
		"target_label": _target_label(target_type, target),
		"expires_at": str(expires_at),
		"url": f"{_support_view_portal_url(target_type)}{TARGETS[target_type]['path']}?token={raw_token}",
	}


def get_support_view_targets(target_type=None, query=None, limit=50):
	_require_school_admin()
	target_type = _validate_target_type(target_type)
	limit = min(max(cint(limit) or 50, 1), 100)
	query = (query or "").strip()
	if target_type == "Campus Admin":
		return {"items": _campus_admin_targets(query, limit)}
	return {"items": _master_targets(target_type, query, limit)}


def get_support_view_context(target_type=None, token=None):
	context = resolve_support_view_token(target_type=target_type, token=token, require_token=True)
	return {
		"read_only": True,
		"target_type": context["target_type"],
		"target": context["target"],
		"target_label": _target_label(context["target_type"], context["target"]),
		"expires_at": str(context["expires_at"]),
	}


def resolve_support_view_token(target_type=None, token=None, require_token=False):
	token = token or get_support_view_token()
	if not token:
		if require_token:
			frappe.throw(_("This support view link is missing its token."), frappe.PermissionError)
		return None
	token_doc = _get_token_doc(token)
	if not token_doc:
		frappe.throw(_("This support view link is not valid."), frappe.PermissionError)
	if token_doc.get("revoked"):
		_log_event(token_doc, "Revoked")
		frappe.db.commit()
		frappe.throw(_("This support view link has expired."), frappe.PermissionError)
	if token_doc.get("expires_at") and token_doc.expires_at <= now_datetime():
		_log_event(token_doc, "Expired")
		frappe.db.commit()
		frappe.throw(_("This support view link has expired."), frappe.PermissionError)
	if target_type and token_doc.get("target_type") != target_type:
		frappe.throw(_("This support view link is not valid for this portal."), frappe.PermissionError)
	if not frappe.db.exists(token_doc.target_doctype, token_doc.target):
		frappe.throw(_("The selected support-view target is no longer available."), frappe.PermissionError)
	if not token_doc.get("last_used_at"):
		_log_event(token_doc, "Opened")
	frappe.db.set_value("Support View Token", token_doc.name, "last_used_at", now_datetime(), update_modified=False)
	frappe.db.commit()
	return {
		"token_name": token_doc.name,
		"target_type": token_doc.target_type,
		"target": token_doc.target,
		"expires_at": token_doc.expires_at,
	}


def get_support_view_token():
	request = getattr(frappe.local, "request", None)
	if not request:
		return ""
	return (request.headers.get(SUPPORT_TOKEN_HEADER) or frappe.form_dict.get("support_token") or "").strip()


def get_support_view_parent():
	context = resolve_support_view_token(target_type="Parent")
	return frappe.get_cached_doc("Parent", context["target"]) if context else None


def get_support_view_teacher():
	context = resolve_support_view_token(target_type="Teacher")
	return frappe.get_cached_doc("Teacher", context["target"]) if context else None


def get_support_view_campus_admin_profile():
	context = resolve_support_view_token(target_type="Campus Admin")
	return frappe.get_doc("Campus Admin Profile", context["target"]) if context else None


def reject_support_view_write():
	token = get_support_view_token()
	if not token:
		return
	token_doc = _get_token_doc(token)
	if token_doc:
		_log_event(token_doc, "Denied")
		frappe.db.commit()
	frappe.throw(_("This is a read-only support view. Changes are not allowed."), frappe.PermissionError)


def _get_token_doc(raw_token):
	name = frappe.db.get_value("Support View Token", {"token_hash": _token_hash(raw_token)}, "name")
	return frappe.get_doc("Support View Token", name) if name else None


def _log_event(token_doc, event):
	if not frappe.db.exists("DocType", "Support View Log"):
		return
	request = getattr(frappe.local, "request", None)
	frappe.get_doc(
		{
			"doctype": "Support View Log",
			"viewer_user": token_doc.viewer_user,
			"target_type": token_doc.target_type,
			"target_doctype": token_doc.target_doctype,
			"target": token_doc.target,
			"support_view_token": token_doc.name,
			"event": event,
			"reason": token_doc.reason,
			"ip_address": request.remote_addr if request else "",
			"user_agent": (request.headers.get("User-Agent") if request else "") or "",
		}
	).insert(ignore_permissions=True)


def _master_targets(target_type, query, limit):
	doctype = TARGETS[target_type]["doctype"]
	label_field = "parent_name" if target_type == "Parent" else "teacher_name"
	fields = ["name", label_field]
	for field in ["status", "email", "email_id", "linked_user", "user"]:
		if _has_field(doctype, field):
			fields.append(field)
	or_filters = None
	if query:
		or_filters = [[doctype, field, "like", f"%{query}%"] for field in fields if field != "status"]
	rows = frappe.get_all(doctype, fields=fields, or_filters=or_filters, order_by=f"{label_field} asc", limit_page_length=limit)
	return [
		{
			"value": row.name,
			"label": row.get(label_field) or row.name,
			"meta": " · ".join(str(row.get(field)) for field in ["email", "email_id", "linked_user", "user", "status"] if row.get(field)),
		}
		for row in rows
	]


def _campus_admin_targets(query, limit):
	profiles = frappe.get_all("Campus Admin Profile", fields=["name", "user", "active"], order_by="user asc", limit_page_length=limit)
	campuses_by_profile = {}
	for row in frappe.get_all("Campus Admin Profile Campus", fields=["parent", "campus"], limit_page_length=0):
		campuses_by_profile.setdefault(row.parent, []).append(row.campus)
	items = []
	for profile in profiles:
		label = profile.user or profile.name
		meta = " · ".join([*campuses_by_profile.get(profile.name, []), "Active" if profile.active else "Inactive"])
		if query and query.lower() not in f"{label} {meta}".lower():
			continue
		items.append({"value": profile.name, "label": label, "meta": meta})
	return items[:limit]


def _target_label(target_type, target):
	doctype = TARGETS[target_type]["doctype"]
	if target_type == "Parent":
		return frappe.db.get_value(doctype, target, "parent_name") or target
	if target_type == "Teacher":
		return frappe.db.get_value(doctype, target, "teacher_name") or target
	return frappe.db.get_value(doctype, target, "user") or target


def _support_view_portal_url(target_type):
	if target_type == "Teacher":
		base_url = (
			frappe.conf.get("qas_teacher_portal_url")
			or frappe.conf.get("teacher_portal_url")
			or frappe.conf.get("teacher_portal_base_url")
			or DEFAULT_TEACHER_PORTAL_URL
		)
	else:
		base_url = (
			frappe.conf.get("qas_parent_portal_url")
			or frappe.conf.get("parent_portal_url")
			or frappe.conf.get("parent_portal_base_url")
			or DEFAULT_PARENT_PORTAL_URL
		)
	return str(base_url).rstrip("/")


def _token_hash(value):
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_target_type(target_type):
	if target_type not in TARGETS:
		frappe.throw(_("Unsupported support-view target type."))
	return target_type


def _has_field(doctype, fieldname):
	return frappe.db.has_column(doctype, fieldname)


def _require_school_admin():
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)
