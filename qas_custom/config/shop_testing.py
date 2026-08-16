"""Temporary allowlist for the parent Shop test.

Remove this module and the calls to ``require_parent_shop_testing`` when the
Shop is ready for its normal parent rollout.
"""

from __future__ import annotations

import frappe
from frappe import _


PARENT_SHOP_TEST_EMAILS = frozenset({"rogerxu130@gmail.com"})


def parent_shop_testing_enabled(user: str | None = None) -> bool:
    user = str(user or frappe.session.user or "").strip()
    if not user or user == "Guest":
        return False
    email = frappe.db.get_value("User", user, "email") or user
    return str(email).strip().lower() in PARENT_SHOP_TEST_EMAILS


def require_parent_shop_testing():
    if not parent_shop_testing_enabled():
        frappe.throw(_("Shop testing is not enabled for this account."), frappe.PermissionError)
