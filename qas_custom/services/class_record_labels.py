from __future__ import annotations

import frappe
from frappe.utils import getdate


DAY_LABELS = {
	"Monday": "Mon",
	"Tuesday": "Tue",
	"Wednesday": "Wed",
	"Thursday": "Thu",
	"Friday": "Fri",
	"Saturday": "Sat",
	"Sunday": "Sun",
}


def _label(doctype, name, fieldname):
	if not name:
		return ""
	return frappe.db.get_value(doctype, name, fieldname) or name


def _time(value):
	if value is None:
		return ""
	if hasattr(value, "total_seconds"):
		minutes = int(value.total_seconds() // 60)
		return f"{minutes // 60:02d}:{minutes % 60:02d}"
	if hasattr(value, "hour"):
		return f"{value.hour:02d}:{value.minute:02d}"
	return str(value).split(".")[0][:5]


def build_weekly_timeslot_label(doc):
	campus = _label("Campus", doc.get("campus"), "campus_name")
	room = _label("Classroom", doc.get("classroom"), "classroom_name")
	location = " ".join(part for part in (campus, room) if part)
	start = _time(doc.get("start_time"))
	end = _time(doc.get("end_time"))
	time_range = "-".join(part for part in (start, end) if part)
	return " · ".join(
		str(part)
		for part in (
			_label("Course", doc.get("course"), "course_name"),
			doc.get("class_language") or "English",
			location,
			DAY_LABELS.get(doc.get("day_of_week"), doc.get("day_of_week")),
			time_range,
			_label("Teacher", doc.get("teacher"), "teacher_name") if doc.get("teacher") else "Unassigned",
			_label("Term", doc.get("term"), "term_name"),
		)
		if part
	)


def build_course_session_label(doc):
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		doc.get("weekly_timeslot"),
		["term", "course", "class_language", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time"],
		as_dict=True,
	) or frappe._dict()
	teacher = doc.get("teacher_override") or timeslot.get("teacher")
	campus = _label("Campus", timeslot.get("campus"), "campus_name")
	room = _label("Classroom", timeslot.get("classroom"), "classroom_name")
	location = " ".join(part for part in (campus, room) if part)
	start = _time(timeslot.get("start_time"))
	end = _time(timeslot.get("end_time"))
	return " · ".join(
		str(part)
		for part in (
			getdate(doc.get("session_date")).strftime("%d %b %Y") if doc.get("session_date") else "",
			"-".join(part for part in (start, end) if part),
			_label("Course", timeslot.get("course"), "course_name"),
			timeslot.get("class_language") or "English",
			location,
			_label("Teacher", teacher, "teacher_name") if teacher else "Unassigned",
		)
		if part
	)


def refresh_linked_course_session_labels(doc, method=None):
	if not doc.get("name") or not frappe.db.exists("DocType", "Course Sessions"):
		return
	for name in frappe.get_all("Course Sessions", filters={"weekly_timeslot": doc.name}, pluck="name", limit_page_length=0):
		session = frappe.get_doc("Course Sessions", name)
		label = build_course_session_label(session)
		if session.get("display_label") != label:
			frappe.db.set_value("Course Sessions", name, "display_label", label, update_modified=False)
