from __future__ import annotations

import frappe


def execute():
	_add_weekly_timeslot_fields()
	_add_course_session_fields()
	frappe.clear_cache()


def _add_weekly_timeslot_fields():
	if not frappe.db.exists("DocType", "Weekly Timeslot"):
		return

	_ensure_custom_field(
		"Weekly Timeslot",
		{
			"fieldname": "revenue_share_section",
			"fieldtype": "Section Break",
			"label": "Teacher-Owned Class",
			"insert_after": _existing_field("Weekly Timeslot", ["teacher", "end_time", "classroom"]),
		},
	)
	_ensure_custom_field(
		"Weekly Timeslot",
		{
			"fieldname": "revenue_share_enabled",
			"fieldtype": "Check",
			"label": "Teacher-Owned Class",
			"default": "0",
			"insert_after": "revenue_share_section",
			"description": "Enable teacher revenue share for all sessions generated from this weekly timeslot unless a session overrides it.",
		},
	)
	_ensure_custom_field(
		"Weekly Timeslot",
		{
			"fieldname": "revenue_share_teacher",
			"fieldtype": "Link",
			"label": "Revenue Share Teacher",
			"options": "Teacher",
			"insert_after": "revenue_share_enabled",
			"depends_on": "eval:doc.revenue_share_enabled",
			"description": "Teacher who owns this class for revenue-share settlement. This does not control teacher portal access.",
		},
	)
	_ensure_custom_field(
		"Weekly Timeslot",
		{
			"fieldname": "revenue_share_percent",
			"fieldtype": "Percent",
			"label": "Revenue Share %",
			"default": "2",
			"insert_after": "revenue_share_teacher",
			"depends_on": "eval:doc.revenue_share_enabled",
		},
	)
	frappe.clear_cache(doctype="Weekly Timeslot")


def _add_course_session_fields():
	if not frappe.db.exists("DocType", "Course Sessions"):
		return

	_ensure_custom_field(
		"Course Sessions",
		{
			"fieldname": "revenue_share_section",
			"fieldtype": "Section Break",
			"label": "Teacher-Owned Session",
			"insert_after": _existing_field("Course Sessions", ["status", "session_date", "weekly_timeslot"]),
		},
	)
	_ensure_custom_field(
		"Course Sessions",
		{
			"fieldname": "revenue_share_override",
			"fieldtype": "Select",
			"label": "Revenue Share Override",
			"options": "Inherit from Weekly Timeslot\nTeacher-Owned\nNot Teacher-Owned",
			"default": "Inherit from Weekly Timeslot",
			"insert_after": "revenue_share_section",
			"description": "Override teacher revenue share for this individual session.",
		},
	)
	_ensure_custom_field(
		"Course Sessions",
		{
			"fieldname": "revenue_share_teacher",
			"fieldtype": "Link",
			"label": "Revenue Share Teacher",
			"options": "Teacher",
			"insert_after": "revenue_share_override",
			"description": "Session-level owner for teacher revenue-share settlement. Leave blank to inherit from the weekly timeslot.",
		},
	)
	_ensure_custom_field(
		"Course Sessions",
		{
			"fieldname": "revenue_share_percent",
			"fieldtype": "Percent",
			"label": "Revenue Share %",
			"insert_after": "revenue_share_teacher",
			"description": "Session-level revenue share percentage. Leave blank to inherit from the weekly timeslot, otherwise default settlement uses 2%.",
		},
	)
	frappe.clear_cache(doctype="Course Sessions")


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
		return
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return

	frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values}).insert(ignore_permissions=True)


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
