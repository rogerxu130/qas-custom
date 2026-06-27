from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate


def build_session_option(session, timeslot):
	return {
		"id": session.name,
		"name": session.name,
		"course_session": session.name,
		"session_date": str(session.session_date) if session.session_date else None,
		"course": timeslot.course,
		"campus": timeslot.campus,
		"classroom": timeslot.classroom,
		"teacher": timeslot.teacher,
		"term": timeslot.term,
		"start_time": str(timeslot.start_time) if timeslot.start_time else None,
		"end_time": str(timeslot.end_time) if timeslot.end_time else None,
		"label": " · ".join(
			str(part)
			for part in [
				session.session_date,
				timeslot.start_time,
				timeslot.course,
				timeslot.campus,
				timeslot.teacher,
			]
			if part
		),
	}


def get_remaining_sessions(weekly_timeslot: str, start_date):
	return frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": weekly_timeslot,
			"session_date": [">=", getdate(start_date)],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, name asc",
	)


def get_session_context(course_session: str):
	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "status"],
		as_dict=True,
	)
	if not session:
		frappe.throw(_("Course session was not found."))
	if not session.get("weekly_timeslot"):
		frappe.throw(_("Course session is missing a weekly timeslot."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.weekly_timeslot,
		["name", "term", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))
	return {"session": session, "timeslot": timeslot}


def get_weekly_timeslot_map(timeslot_ids: list[str]):
	if not timeslot_ids:
		return {}

	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", timeslot_ids]},
		fields=["name", "course", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time"],
	)
	return {row["name"]: row for row in rows}


def get_teacher_name_map(teacher_ids: list[str]):
	if not teacher_ids:
		return {}

	rows = frappe.get_all("Teacher", filters={"name": ["in", teacher_ids]}, fields=["name", "teacher_name"])
	return {row["name"]: row.get("teacher_name") or row["name"] for row in rows}
