from __future__ import annotations

import hashlib
import json
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, escape_html, now_datetime

from qas_custom.services.password_reset import (
	PASSWORD_RESET_TOKEN_DOCTYPE,
	_build_password_reset_link,
)
from qas_custom.services.school_admin import _require_school_admin

PORTAL_INVITE_EXPIRY_DAYS = 7
PARENT_PORTAL_ROLE = "Parent"
INVITE_LOG_DOCTYPE = "Parent Portal Invite Log"


def invite_parent_to_portal_data(parent=None):
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	result = _invite_parent_to_portal(parent, source="Manual")
	frappe.db.commit()
	return result


def bulk_invite_parents_to_portal_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	parents = payload.get("parents") or []
	parents = [str(parent).strip() for parent in parents if str(parent or "").strip()]
	if not parents:
		frappe.throw(_("At least one parent is required."))
	parents = list(dict.fromkeys(parents))[:300]

	items = []
	for parent in parents:
		try:
			invite_status = get_parent_portal_invite_status(parent)
			if invite_status.get("status") != "never_invited":
				items.append({
					"parent": parent,
					"sent": False,
					"status": "skipped",
					"reason": invite_status.get("reason") or _("Already invited or logged in."),
					"portal_invite_status": invite_status,
				})
				continue
			items.append(_invite_parent_to_portal(parent, source="Bulk Never Invited"))
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), f"QAS parent portal invite failed: {parent}")
			items.append({"parent": parent, "sent": False, "status": "failed", "reason": str(exc)})
	frappe.db.commit()
	return {
		"count": len(items),
		"sent": len([row for row in items if row.get("sent")]),
		"failed": len([row for row in items if row.get("status") == "failed"]),
		"skipped": len([row for row in items if row.get("status") == "skipped"]),
		"items": items,
	}


def _invite_parent_to_portal(parent, source="Manual"):
	parent_doc = frappe.get_doc("Parent", parent)
	parent_name = parent_doc.get("parent_name") or parent_doc.name
	email = _parent_email(parent_doc)
	if not email:
		return {"parent": parent_doc.name, "parent_name": parent_name, "sent": False, "status": "skipped", "reason": "No parent email found."}

	user_name = _ensure_parent_portal_user(parent_doc, email, parent_name)
	invite = _create_invite_token(user_name, email)
	_send_parent_portal_invite_email(parent_name, email, invite["reset_link"], invite["expires_at"], invite["token_record"])
	_log_parent_portal_invite(parent_doc, user_name, email, invite, source)
	_add_parent_comment(parent_doc.name, _("Parent Portal invite sent to {0}.").format(email))
	return {
		"parent": parent_doc.name,
		"parent_name": parent_name,
		"linked_user": user_name,
		"email": email,
		"sent": True,
		"status": "sent",
		"token_record": invite["token_record"],
		"expires_at": invite["expires_at"],
		"portal_invite_status": get_parent_portal_invite_status(parent_doc.name),
	}


def _ensure_parent_portal_user(parent_doc, email, parent_name):
	user_name = parent_doc.get("linked_user")
	if user_name and not frappe.db.exists("User", user_name):
		user_name = None
	if not user_name:
		user_name = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	if not user_name:
		user_doc = frappe.new_doc("User")
		user_doc.email = email
		user_doc.first_name = parent_name or email
		user_doc.enabled = 1
		user_doc.user_type = "Website User"
		user_doc.send_welcome_email = 0
		user_doc.flags.ignore_permissions = True
		user_doc.insert(ignore_permissions=True)
		user_name = user_doc.name

	user_doc = frappe.get_doc("User", user_name)
	changed = False
	if not user_doc.get("enabled"):
		user_doc.enabled = 1
		changed = True
	if user_doc.get("user_type") != "Website User":
		user_doc.user_type = "Website User"
		changed = True
	if _ensure_user_role(user_doc, PARENT_PORTAL_ROLE):
		changed = True
	if changed:
		user_doc.flags.ignore_permissions = True
		user_doc.save(ignore_permissions=True)

	if parent_doc.meta.has_field("linked_user") and parent_doc.get("linked_user") != user_name:
		parent_doc.linked_user = user_name
		parent_doc.flags.ignore_permissions = True
		parent_doc.save(ignore_permissions=True)
	return user_name


def _ensure_user_role(user_doc, role):
	if not frappe.db.exists("Role", role):
		frappe.throw(_("Role {0} is required for Parent Portal access.").format(role))
	current_roles = {row.role for row in user_doc.get("roles", []) if row.get("role")}
	if role in current_roles:
		return False
	user_doc.append("roles", {"role": role})
	return True


def get_parent_portal_invite_status(parent):
	parent_doc = frappe.get_doc("Parent", parent) if isinstance(parent, str) else parent
	parent_name = parent_doc.get("parent_name") or parent_doc.get("name")
	email = _parent_email(parent_doc)
	linked_user = _parent_linked_user(parent_doc, email)
	login_state = _parent_login_state(linked_user)
	history = _parent_invite_history(parent_doc.get("name"))

	if login_state.get("has_logged_in"):
		status = "logged_in"
		label = _("Logged in")
		reason = _("Parent has already logged in.")
	elif history.get("invited"):
		status = "invited_not_logged_in"
		label = _("Invited, not logged in")
		reason = _("Parent has already been invited.")
	elif not email:
		status = "no_email"
		label = _("No email")
		reason = _("No parent email found.")
	else:
		status = "never_invited"
		label = _("Never invited")
		reason = ""

	return {
		"status": status,
		"label": label,
		"reason": reason,
		"parent": parent_doc.get("name"),
		"parent_name": parent_name,
		"email": email,
		"linked_user": linked_user,
		"invited": bool(history.get("invited")),
		"invite_count": history.get("invite_count") or 0,
		"last_invited_at": history.get("last_invited_at"),
		"last_invite": history.get("last_invite"),
		"has_logged_in": bool(login_state.get("has_logged_in")),
		"last_login": login_state.get("last_login"),
		"last_active": login_state.get("last_active"),
		"bulk_eligible": status == "never_invited",
	}


def _parent_linked_user(parent_doc, email=None):
	linked_user = parent_doc.get("linked_user")
	if linked_user and frappe.db.exists("User", linked_user):
		return linked_user
	if email:
		return frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	return ""


def _parent_login_state(user_name):
	if not user_name or not frappe.db.exists("User", user_name):
		return {"has_logged_in": False, "last_login": None, "last_active": None}
	fields = [fieldname for fieldname in ["last_login", "last_active"] if frappe.get_meta("User").has_field(fieldname)]
	if not fields:
		return {"has_logged_in": False, "last_login": None, "last_active": None}
	values = frappe.db.get_value("User", user_name, fields, as_dict=True) or {}
	last_login = values.get("last_login")
	last_active = values.get("last_active")
	return {
		"has_logged_in": bool(last_login or last_active),
		"last_login": str(last_login) if last_login else None,
		"last_active": str(last_active) if last_active else None,
	}


def _parent_invite_history(parent):
	if not parent:
		return {"invited": False, "invite_count": 0}
	log_history = _parent_invite_log_history(parent)
	if log_history.get("invited"):
		return log_history
	comment_history = _parent_invite_comment_history(parent)
	if comment_history.get("invited"):
		return comment_history
	return {"invited": False, "invite_count": 0}


def _parent_invite_log_history(parent):
	if not frappe.db.exists("DocType", INVITE_LOG_DOCTYPE):
		return {"invited": False, "invite_count": 0}
	count = frappe.db.count(INVITE_LOG_DOCTYPE, {"parent": parent})
	if not count:
		return {"invited": False, "invite_count": 0}
	latest = frappe.get_all(
		INVITE_LOG_DOCTYPE,
		filters={"parent": parent},
		fields=["name", "sent_at"],
		order_by="sent_at desc, creation desc",
		limit=1,
	)
	return {
		"invited": True,
		"invite_count": count,
		"last_invite": latest[0].name if latest else None,
		"last_invited_at": str(latest[0].sent_at) if latest and latest[0].sent_at else None,
	}


def _parent_invite_comment_history(parent):
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Parent",
			"reference_name": parent,
			"content": ["like", "%Parent Portal invite sent%"],
		},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {"invited": False, "invite_count": 0}
	return {
		"invited": True,
		"invite_count": 1,
		"last_invite": rows[0].name,
		"last_invited_at": str(rows[0].creation) if rows[0].creation else None,
	}


def _log_parent_portal_invite(parent_doc, user_name, email, invite, source):
	if not frappe.db.exists("DocType", INVITE_LOG_DOCTYPE):
		return
	frappe.get_doc({
		"doctype": INVITE_LOG_DOCTYPE,
		"parent": parent_doc.name,
		"parent_name": parent_doc.get("parent_name") or parent_doc.name,
		"user": user_name,
		"email": email,
		"sent_at": now_datetime(),
		"source": source or "Manual",
		"status": "Sent",
		"token_record": invite.get("token_record"),
		"expires_at": invite.get("expires_at"),
		"sent_by": frappe.session.user,
	}).insert(ignore_permissions=True)


def _create_invite_token(user_name, email):
	if not frappe.db.exists("DocType", PASSWORD_RESET_TOKEN_DOCTYPE):
		frappe.throw(_("Portal password reset tokens are not installed."))
	now = now_datetime()
	expires_at = add_to_date(now, days=PORTAL_INVITE_EXPIRY_DAYS, as_datetime=True)
	reset_token = secrets.token_urlsafe(32)
	token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
	doc = frappe.get_doc({
		"doctype": PASSWORD_RESET_TOKEN_DOCTYPE,
		"user": user_name,
		"email": email,
		"token_hash": token_hash,
		"status": "Pending",
		"requested_at": now,
		"expires_at": expires_at,
		"request_ip": getattr(frappe.local, "request_ip", None),
	})
	doc.insert(ignore_permissions=True)
	return {"token_record": doc.name, "reset_link": _build_password_reset_link(reset_token), "expires_at": expires_at}


def _send_parent_portal_invite_email(parent_name, email, reset_link, expires_at, token_record):
	subject = _("Welcome to Queensland Art School Parent Portal")
	greeting = _("Hi {0},").format(parent_name) if parent_name else _("Hi,")
	safe_greeting = escape_html(greeting)
	safe_reset_link = escape_html(reset_link)
	safe_expires_at = escape_html(str(expires_at))
	message = f"""
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">Queensland Art School</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Set up your Parent Portal</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{safe_greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Queensland Art School has created a Parent Portal account for your family. Please use the secure link below to choose your password and sign in.</p>
						<p style="margin:0 0 22px;"><a href="{safe_reset_link}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">Set password</a></p>
						<p style="margin:0 0 10px;font-size:13px;line-height:1.5;color:#64748b;">This link expires at {safe_expires_at}.</p>
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If the button does not work, copy this link into your browser:<br>{safe_reset_link}</p>
					</div>
				</div>
			</div>
		</div>
	"""
	try:
		frappe.sendmail(recipients=[email], subject=subject, message=message, delayed=False)
	except Exception:
		frappe.log_error(
			title="Parent Portal Invite Email Delivery Failed",
			message=frappe.get_traceback() + f"\n\nToken Record: {token_record}\nRecipient: {email}\nInvite Link: {reset_link}",
		)
		raise


def _parent_email(parent_doc):
	for fieldname in ["email", "email_id", "contact_email", "linked_user"]:
		value = parent_doc.get(fieldname)
		if value:
			if fieldname == "linked_user" and frappe.db.exists("User", value):
				return (frappe.db.get_value("User", value, "email") or value or "").strip().lower()
			return str(value).strip().lower()
	return ""


def _add_parent_comment(parent, message):
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "Parent",
		"reference_name": parent,
		"content": message,
	}).insert(ignore_permissions=True)


def _get_payload(payload):
	if isinstance(payload, str):
		return json.loads(payload or "{}")
	return payload or {}
