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


def invite_parent_to_portal_data(parent=None):
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	result = _invite_parent_to_portal(parent)
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
			items.append(_invite_parent_to_portal(parent))
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


def _invite_parent_to_portal(parent):
	parent_doc = frappe.get_doc("Parent", parent)
	parent_name = parent_doc.get("parent_name") or parent_doc.name
	email = _parent_email(parent_doc)
	if not email:
		return {"parent": parent_doc.name, "parent_name": parent_name, "sent": False, "status": "skipped", "reason": "No parent email found."}

	user_name = _ensure_parent_portal_user(parent_doc, email, parent_name)
	invite = _create_invite_token(user_name, email)
	_send_parent_portal_invite_email(parent_name, email, invite["reset_link"], invite["expires_at"], invite["token_record"])
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
