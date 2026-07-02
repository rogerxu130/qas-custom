from __future__ import annotations

import frappe


QAS_ROLES = (
	{"role_name": "School Admin", "desk_access": 1},
	{"role_name": "Parent", "desk_access": 0},
)


def execute():
	if not frappe.db.exists("DocType", "Role"):
		return

	for role in QAS_ROLES:
		_ensure_role(role["role_name"], desk_access=role["desk_access"])


def _ensure_role(role_name: str, *, desk_access: int):
	if frappe.db.exists("Role", role_name):
		return

	doc = frappe.new_doc("Role")
	doc.role_name = role_name
	doc.desk_access = desk_access
	doc.disabled = 0
	doc.two_factor_auth = 0
	doc.insert(ignore_permissions=True)
