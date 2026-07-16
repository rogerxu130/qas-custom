from __future__ import annotations

import frappe

from qas_custom.patches.v2026_07_02_ensure_qas_roles import _ensure_role


CAMPUS_ADMIN_ROLE = "Campus Admin"


def execute():
	if not _role_doctype_exists():
		return

	_ensure_role(CAMPUS_ADMIN_ROLE, desk_access=0)


def _role_doctype_exists():
	return bool(frappe.db.exists("DocType", "Role"))
