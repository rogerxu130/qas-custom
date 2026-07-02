from __future__ import annotations

import frappe


STUDENT_SYSTEM_MANAGER_PERMISSION = {
	"select": 0,
	"read": 1,
	"write": 1,
	"create": 1,
	"delete": 1,
	"submit": 0,
	"cancel": 0,
	"amend": 0,
	"report": 1,
	"export": 1,
	"import": 0,
	"share": 1,
	"print": 1,
	"email": 1,
}


def execute():
	if not frappe.db.exists("DocType", "Student") or not frappe.db.exists("Role", "System Manager"):
		return

	_ensure_student_system_manager_permission()
	frappe.clear_cache(doctype="Student")


def _ensure_student_system_manager_permission():
	name = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": "Student", "role": "System Manager", "permlevel": 0, "if_owner": 0},
		"name",
	)
	if not name:
		from frappe.permissions import add_permission

		add_permission("Student", "System Manager", 0, "read")
		name = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": "Student", "role": "System Manager", "permlevel": 0, "if_owner": 0},
			"name",
		)

	if name:
		frappe.db.set_value(
			"Custom DocPerm",
			name,
			STUDENT_SYSTEM_MANAGER_PERMISSION,
			update_modified=False,
		)
