from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, escape_html, validate_email_address

from qas_custom.services.password_reset import PORTAL_CAMPUS_ADMIN
from qas_custom.utils.environment import sendmail_or_skip


CAMPUS_ADMIN_ROLE = "Campus Admin"
CAMPUS_ADMIN_INVITE_MARKER = "Campus Admin Portal invite sent"


def get_campus_admin_accounts_data(query=None):
	_require_school_admin()
	query = (query or "").strip().lower()
	items = [_build_campus_admin_payload(row.name) for row in _get_profile_rows()]
	if query:
		items = [row for row in items if _account_matches_query(row, query)]
	return {"items": items, "campuses": _get_available_campuses()}


def get_campus_admin_account_data(profile=None):
	_require_school_admin()
	if not profile or not frappe.db.exists("Campus Admin Profile", profile):
		frappe.throw(_("Campus Admin profile was not found."))
	return _build_campus_admin_payload(profile)


def save_campus_admin_account_data(profile=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	profile = profile or payload.get("profile")
	display_name = (payload.get("display_name") or "").strip()
	email = _normalise_email(payload.get("email"))
	campuses = _normalise_campuses(payload.get("campuses"))
	active = cint(payload.get("active", 1)) == 1

	if not display_name:
		frappe.throw(_("Campus Admin name is required."))
	if not email:
		frappe.throw(_("Campus Admin email is required."))
	validate_email_address(email, throw=True)
	_validate_campuses(campuses)
	_require_campus_admin_role()

	if profile:
		profile_doc = frappe.get_doc("Campus Admin Profile", profile)
		user_doc = frappe.get_doc("User", profile_doc.user)
		if _normalise_email(user_doc.get("email") or user_doc.name) != email:
			frappe.throw(_("Campus Admin email cannot be changed after the account is created."))
		_update_user_name(user_doc, display_name)
		_ensure_campus_admin_role(user_doc)
		profile_doc.active = 1 if active else 0
		profile_doc.set("campuses", [{"campus": campus} for campus in campuses])
		profile_doc.flags.ignore_permissions = True
		profile_doc.save(ignore_permissions=True)
	else:
		user_doc = _get_or_create_user(email, display_name)
		existing_profile = frappe.db.get_value("Campus Admin Profile", {"user": user_doc.name}, "name")
		if existing_profile:
			frappe.throw(_("This User is already linked to Campus Admin profile {0}.").format(existing_profile))
		_ensure_campus_admin_role(user_doc)
		profile_doc = frappe.new_doc("Campus Admin Profile")
		profile_doc.user = user_doc.name
		profile_doc.active = 1 if active else 0
		profile_doc.set("campuses", [{"campus": campus} for campus in campuses])
		profile_doc.flags.ignore_permissions = True
		profile_doc.insert(ignore_permissions=True)

	frappe.db.commit()
	return _build_campus_admin_payload(profile_doc.name)


def set_campus_admin_account_active_data(profile=None, active=1):
	_require_school_admin()
	if not profile or not frappe.db.exists("Campus Admin Profile", profile):
		frappe.throw(_("Campus Admin profile was not found."))
	profile_doc = frappe.get_doc("Campus Admin Profile", profile)
	profile_doc.active = 1 if cint(active) else 0
	profile_doc.flags.ignore_permissions = True
	profile_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _build_campus_admin_payload(profile_doc.name)


def invite_campus_admin_account_data(profile=None):
	_require_school_admin()
	from qas_custom.services.portal_invites import _create_invite_token
	if not profile or not frappe.db.exists("Campus Admin Profile", profile):
		frappe.throw(_("Campus Admin profile was not found."))

	profile_doc = frappe.get_doc("Campus Admin Profile", profile)
	if not cint(profile_doc.active):
		frappe.throw(_("Inactive Campus Admin accounts cannot be invited."))
	user_doc = frappe.get_doc("User", profile_doc.user)
	if not cint(user_doc.enabled):
		frappe.throw(_("The linked User is disabled. Resolve that User before sending an invitation."))
	if _user_has_logged_in(user_doc):
		frappe.throw(_("This Campus Admin has already logged in. Use password reset instead of resending an invitation."))

	email = _normalise_email(user_doc.get("email") or user_doc.name)
	if not email:
		frappe.throw(_("Campus Admin email is required before sending an invitation."))
	validate_email_address(email, throw=True)
	display_name = _user_display_name(user_doc)
	invite = _create_invite_token(user_doc.name, email, portal=PORTAL_CAMPUS_ADMIN)
	mail_result = _send_campus_admin_invite_email(
		display_name,
		email,
		invite["reset_link"],
		invite["expires_at"],
		invite["token_record"],
	)
	if mail_result and mail_result.get("skipped"):
		return {
			"sent": False,
			"profile": profile_doc.name,
			"email": email,
			"reason": mail_result.get("reason") or _("Email delivery was skipped."),
			"portal_invite_status": _get_campus_admin_invite_status(profile_doc, user_doc),
		}

	_add_invite_comment(profile_doc.name, email, invite)
	frappe.db.commit()
	return {
		"sent": True,
		"profile": profile_doc.name,
		"email": email,
		"token_record": invite["token_record"],
		"expires_at": invite["expires_at"],
		"portal_invite_status": _get_campus_admin_invite_status(profile_doc, user_doc),
	}


def get_active_campus_admin_emails(campus):
	"""Return unique enabled User emails for active Profiles assigned to one campus."""
	if not campus or not _campus_admin_profile_available():
		return []
	profile_names = _get_assigned_profile_names(campus)
	if not profile_names:
		return []
	user_names = _get_active_profile_users(profile_names)
	if not user_names:
		return []
	return sorted(set(_get_enabled_user_emails(user_names)))


def _campus_admin_profile_available():
	return bool(frappe.db.exists("DocType", "Campus Admin Profile"))


def _get_assigned_profile_names(campus):
	profile_names = frappe.get_all(
		"Campus Admin Profile Campus",
		filters={"campus": campus, "parenttype": "Campus Admin Profile"},
		pluck="parent",
		limit_page_length=0,
	)
	return list(dict.fromkeys(profile_names))


def _get_active_profile_users(profile_names):
	profiles = frappe.get_all(
		"Campus Admin Profile",
		filters={"name": ["in", profile_names], "active": 1},
		fields=["user"],
		limit_page_length=0,
	)
	return list({row.get("user") for row in profiles if row.get("user")})


def _get_enabled_user_emails(user_names):
	users = frappe.get_all(
		"User",
		filters={"name": ["in", user_names], "enabled": 1},
		fields=["name", "email"],
		limit_page_length=0,
	)
	return [_normalise_email(row.get("email") or row.get("name")) for row in users if _normalise_email(row.get("email") or row.get("name"))]


def _get_profile_rows():
	return frappe.get_all(
		"Campus Admin Profile",
		fields=["name", "user", "active", "modified"],
		order_by="modified desc",
		limit_page_length=0,
	)


def _build_campus_admin_payload(profile):
	profile_doc = frappe.get_doc("Campus Admin Profile", profile)
	user_doc = frappe.get_doc("User", profile_doc.user)
	return {
		"profile": profile_doc.name,
		"user": user_doc.name,
		"display_name": _user_display_name(user_doc),
		"email": _normalise_email(user_doc.get("email") or user_doc.name),
		"active": bool(cint(profile_doc.active)),
		"user_enabled": bool(cint(user_doc.enabled)),
		"campuses": [row.campus for row in profile_doc.get("campuses", []) if row.campus],
		"portal_invite_status": _get_campus_admin_invite_status(profile_doc, user_doc),
		"modified": str(profile_doc.modified) if profile_doc.get("modified") else None,
	}


def _get_campus_admin_invite_status(profile_doc, user_doc):
	login = _user_login_state(user_doc)
	history = _invite_history(profile_doc.name)
	if login["has_logged_in"]:
		status, label = "logged_in", _("Logged in")
	elif history["invited"]:
		status, label = "invited_not_logged_in", _("Invited, not logged in")
	else:
		status, label = "never_invited", _("Never invited")
	return {
		"status": status,
		"label": label,
		"last_login": login["last_login"],
		"last_active": login["last_active"],
		"last_invited_at": history.get("last_invited_at"),
		"invite_count": history.get("invite_count", 0),
	}


def _get_or_create_user(email, display_name):
	user_name = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	if user_name:
		user_doc = frappe.get_doc("User", user_name)
		if not cint(user_doc.enabled):
			frappe.throw(_("An existing User with this email is disabled. Resolve that User before creating a Campus Admin profile."))
		_update_user_name(user_doc, display_name)
		return user_doc

	user_doc = frappe.new_doc("User")
	user_doc.email = email
	user_doc.first_name = display_name
	user_doc.enabled = 1
	user_doc.user_type = "Website User"
	user_doc.send_welcome_email = 0
	user_doc.flags.ignore_permissions = True
	user_doc.insert(ignore_permissions=True)
	return user_doc


def _update_user_name(user_doc, display_name):
	if user_doc.get("first_name") == display_name:
		return
	user_doc.first_name = display_name
	user_doc.flags.ignore_permissions = True
	user_doc.save(ignore_permissions=True)


def _ensure_campus_admin_role(user_doc):
	_require_campus_admin_role()
	roles = {row.role for row in user_doc.get("roles", []) if row.get("role")}
	if CAMPUS_ADMIN_ROLE in roles:
		return
	user_doc.append("roles", {"role": CAMPUS_ADMIN_ROLE})
	user_doc.flags.ignore_permissions = True
	user_doc.save(ignore_permissions=True)


def _require_campus_admin_role():
	if _campus_admin_role_exists():
		return
	frappe.throw(
		_("Campus Admin role is not installed. Update QAS Custom and run site migration before saving Campus Admin accounts.")
	)


def _campus_admin_role_exists():
	return bool(frappe.db.exists("Role", CAMPUS_ADMIN_ROLE))


def _validate_campuses(campuses):
	if not campuses:
		frappe.throw(_("At least one campus is required."))
	available = set(_get_available_campuses())
	unknown = [campus for campus in campuses if campus not in available]
	if unknown:
		frappe.throw(_("Invalid or inactive campus: {0}").format(", ".join(unknown)))


def _get_available_campuses():
	filters = {}
	if frappe.get_meta("Campus").has_field("status"):
		filters["status"] = ["!=", "Inactive"]
	return frappe.get_all("Campus", filters=filters, pluck="name", order_by="name asc", limit_page_length=0)


def _normalise_campuses(value):
	if isinstance(value, str):
		try:
			decoded = json.loads(value)
			value = decoded if isinstance(decoded, list) else [value]
		except (TypeError, ValueError):
			value = [part.strip() for part in value.split(",")]
	return list(dict.fromkeys(str(campus).strip() for campus in (value or []) if str(campus or "").strip()))


def _normalise_email(value):
	return str(value or "").strip().lower()


def _user_display_name(user_doc):
	return (user_doc.get("full_name") or " ".join(part for part in [user_doc.get("first_name"), user_doc.get("last_name")] if part) or user_doc.get("name") or "").strip()


def _user_has_logged_in(user_doc):
	return bool(user_doc.get("last_login") or user_doc.get("last_active"))


def _user_login_state(user_doc):
	last_login = user_doc.get("last_login")
	last_active = user_doc.get("last_active")
	return {
		"has_logged_in": bool(last_login or last_active),
		"last_login": str(last_login) if last_login else None,
		"last_active": str(last_active) if last_active else None,
	}


def _invite_history(profile):
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Campus Admin Profile",
			"reference_name": profile,
			"content": ["like", f"%{CAMPUS_ADMIN_INVITE_MARKER}%"],
		},
		fields=["name", "creation"],
		order_by="creation desc",
		limit_page_length=0,
	)
	return {
		"invited": bool(rows),
		"invite_count": len(rows),
		"last_invited_at": str(rows[0].creation) if rows and rows[0].creation else None,
	}


def _add_invite_comment(profile, email, invite):
	content = _("{0} to {1}. Token record: {2}. Expires: {3}.").format(
		CAMPUS_ADMIN_INVITE_MARKER,
		email,
		invite.get("token_record") or "-",
		invite.get("expires_at") or "-",
	)
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "Campus Admin Profile",
		"reference_name": profile,
		"content": content,
	}).insert(ignore_permissions=True)


def _send_campus_admin_invite_email(display_name, email, reset_link, expires_at, token_record):
	safe_name = escape_html(display_name or "")
	safe_link = escape_html(reset_link)
	safe_expiry = escape_html(str(expires_at))
	message = f"""
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">Queensland Art School</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Set up your Campus Admin Portal</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">Hi {safe_name},</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Queensland Art School has created a Campus Admin Portal account for you. Use the secure link below to choose your password.</p>
						<p style="margin:0 0 22px;"><a href="{safe_link}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">Set password</a></p>
						<p style="margin:0 0 10px;font-size:13px;line-height:1.5;color:#64748b;">This link expires at {safe_expiry}.</p>
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If the button does not work, copy this link into your browser:<br>{safe_link}</p>
					</div>
				</div>
			</div>
		</div>
	"""
	try:
		return sendmail_or_skip(
			action="campus_admin_portal_invite",
			recipients=[email],
			subject=_("Welcome to Queensland Art School Campus Admin Portal"),
			message=message,
			delayed=False,
		)
	except Exception:
		frappe.log_error(
			title="Campus Admin Portal Invite Email Delivery Failed",
			message=frappe.get_traceback() + f"\n\nToken Record: {token_record}\nRecipient: {email}\nInvite Link: {reset_link}",
		)
		raise


def _account_matches_query(row, query):
	haystack = " ".join([
		row.get("display_name") or "",
		row.get("email") or "",
		" ".join(row.get("campuses") or []),
		(row.get("portal_invite_status") or {}).get("label") or "",
	]).lower()
	return query in haystack


def _get_payload(payload=None):
	if isinstance(payload, str):
		return json.loads(payload)
	if isinstance(payload, dict):
		return payload
	request_json = getattr(frappe.request, "json", None) if getattr(frappe.local, "request", None) else None
	return request_json or frappe.form_dict or {}


def _require_school_admin():
	from qas_custom.services.school_admin import _require_school_admin as require_school_admin

	require_school_admin()
