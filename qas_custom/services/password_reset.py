import hashlib
import secrets

import frappe
from frappe.utils import add_to_date, get_url, now_datetime


PASSWORD_RESET_TOKEN_DOCTYPE = "Portal Password Reset Token"
PASSWORD_RESET_EXPIRY_MINUTES = 30
PASSWORD_RESET_PATH = "/reset-password"
DEFAULT_PARENT_PORTAL_ALLOWED_ROLES = ("Parent",)


def request_password_reset(email: str | None) -> dict:
    normalized_email = (email or "").strip().lower()
    generic_response = {
        "ok": True,
        "message": "If the account exists in the system, a password reset link has been sent.",
    }

    if not normalized_email:
        return generic_response

    user_name = frappe.db.get_value(
        "User",
        {"email": normalized_email, "enabled": 1},
        "name",
    )
    if not user_name:
        return generic_response

    if not _is_parent_portal_user(user_name):
        return generic_response

    now = now_datetime()
    expires_at = add_to_date(now, minutes=PASSWORD_RESET_EXPIRY_MINUTES, as_datetime=True)
    reset_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()

    doc = frappe.get_doc(
        {
            "doctype": PASSWORD_RESET_TOKEN_DOCTYPE,
            "user": user_name,
            "email": normalized_email,
            "token_hash": token_hash,
            "status": "Pending",
            "requested_at": now,
            "expires_at": expires_at,
            "request_ip": frappe.local.request_ip,
        }
    )
    doc.insert(ignore_permissions=True)

    reset_link = _build_password_reset_link(reset_token)
    _send_password_reset_email(normalized_email, reset_link, expires_at, doc.name)

    frappe.db.commit()
    return generic_response


def validate_password_reset_token(token: str | None) -> dict:
    normalized_token = (token or "").strip()
    if not normalized_token:
        return {
            "ok": False,
            "valid": False,
            "message": "Reset token is required.",
        }

    token_hash = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()
    token_doc = frappe.db.get_value(
        PASSWORD_RESET_TOKEN_DOCTYPE,
        {"token_hash": token_hash},
        ["name", "user", "status", "expires_at", "used_at"],
        as_dict=True,
    )

    if not token_doc:
        return {
            "ok": False,
            "valid": False,
            "message": "Invalid reset token.",
        }

    if token_doc.status != "Pending":
        return {
            "ok": False,
            "valid": False,
            "message": f"Reset token is not usable because its status is {token_doc.status}.",
        }

    if not _is_parent_portal_user(token_doc.user):
        return {
            "ok": False,
            "valid": False,
            "message": "This reset token is no longer valid for this account.",
        }

    now = now_datetime()
    if token_doc.expires_at and token_doc.expires_at < now:
        frappe.db.set_value(
            PASSWORD_RESET_TOKEN_DOCTYPE,
            token_doc.name,
            "status",
            "Expired",
            update_modified=False,
        )
        frappe.db.commit()
        return {
            "ok": False,
            "valid": False,
            "message": "Reset token has expired.",
        }

    return {
        "ok": True,
        "valid": True,
        "message": "Reset token is valid.",
        "token_record": token_doc.name,
        "user": token_doc.user,
        "expires_at": token_doc.expires_at,
    }


def confirm_password_reset(token: str | None, new_password: str | None) -> dict:
    validation = validate_password_reset_token(token)
    if not validation.get("valid"):
        return validation

    normalized_password = (new_password or "").strip()
    if not normalized_password:
        return {
            "ok": False,
            "message": "New password is required.",
        }

    if len(normalized_password) < 8:
        return {
            "ok": False,
            "message": "New password must be at least 8 characters long.",
        }

    user_name = validation["user"]
    token_record = validation["token_record"]

    user_doc = frappe.get_doc("User", user_name)
    user_doc.new_password = normalized_password
    user_doc.save(ignore_permissions=True)

    frappe.db.set_value(
        PASSWORD_RESET_TOKEN_DOCTYPE,
        token_record,
        {
            "status": "Used",
            "used_at": now_datetime(),
        },
        update_modified=False,
    )
    frappe.db.commit()

    return {
        "ok": True,
        "message": "Password has been reset successfully.",
        "token_record": token_record,
        "user": user_name,
    }


def _build_password_reset_link(reset_token: str) -> str:
    portal_base_url = _get_parent_portal_base_url()
    if portal_base_url:
        return f"{portal_base_url.rstrip('/')}{PASSWORD_RESET_PATH}?token={reset_token}"

    return f"{get_url(PASSWORD_RESET_PATH)}?token={reset_token}"


def _send_password_reset_email(email: str, reset_link: str, expires_at, token_record: str) -> None:
    subject = "Reset your password"
    message = f"""
        <p>A password reset was requested for your account.</p>
        <p>Use the link below to set a new password:</p>
        <p><a href="{reset_link}">{reset_link}</a></p>
        <p>This link will expire at {expires_at}.</p>
        <p>If you did not request this, you can ignore this email.</p>
    """

    try:
        frappe.sendmail(
            recipients=[email],
            subject=subject,
            message=message,
            delayed=False,
        )
    except Exception:
        frappe.log_error(
            title="Password Reset Email Delivery Failed",
            message=frappe.get_traceback()
            + f"\n\nToken Record: {token_record}\nRecipient: {email}\nReset Link: {reset_link}",
        )


def _is_parent_portal_user(user_name: str) -> bool:
    user_type = frappe.db.get_value("User", user_name, "user_type")
    if user_type != "Website User":
        return False

    user_roles = set(frappe.get_roles(user_name))
    allowed_roles = set(_get_parent_portal_allowed_roles())
    return bool(user_roles.intersection(allowed_roles))


def _get_parent_portal_allowed_roles() -> tuple[str, ...]:
    configured_roles = getattr(frappe.conf, "parent_portal_reset_allowed_roles", None)

    if isinstance(configured_roles, str) and configured_roles.strip():
        roles = tuple(role.strip() for role in configured_roles.split(",") if role.strip())
        if roles:
            return roles

    if isinstance(configured_roles, (list, tuple)):
        roles = tuple(str(role).strip() for role in configured_roles if str(role).strip())
        if roles:
            return roles

    return DEFAULT_PARENT_PORTAL_ALLOWED_ROLES


def _get_parent_portal_base_url() -> str | None:
    configured_url = getattr(frappe.conf, "parent_portal_base_url", None)
    if isinstance(configured_url, str) and configured_url.strip():
        return configured_url.strip().rstrip("/")

    return None
