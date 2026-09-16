"""Export the three CSV inputs consumed by the standalone QAS timetable planner."""
from __future__ import annotations

import csv
from datetime import time, timedelta
from io import BytesIO, StringIO
import re
from zipfile import ZIP_DEFLATED, ZipFile

import frappe
from frappe import _
from frappe.utils import cint


COURSE_HEADERS = (
	"course_id", "course_name_bilingual", "course_name_en", "course_name_zh",
	"suitable_age_en", "suitable_age_zh", "session_length_hours", "session_length_minutes",
	"sessions_per_term", "total_hours_per_term", "duration_summary_en", "trial_price_aud", "term_price_aud",
)
SESSION_HEADERS = ("course_id", "language", "teacher_name", "room", "start_time", "campus", "weekday")
WEEKDAYS = {day: day[:3].lower() for day in (
	"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)}
CAMPUS_KEYS = {"indooroopilly": "indooroopilly", "uppermountgravatt": "upper_mount_gravatt"}


def export_school_admin_timetable_data(term=None):
	if frappe.session.user == "Guest" or not {"School Admin", "System Manager"}.intersection(
		frappe.get_roles(frappe.session.user)
	):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)
	if not term or not frappe.db.exists("Term", term):
		frappe.throw(_("Please select an existing Term."))

	sessions = frappe.get_all(
		"Weekly Timeslot", filters={"term": term, "status": "Active"},
		fields=["name", "course", "class_language", "campus", "classroom", "teacher", "day_of_week", "start_time"],
		order_by="name asc", limit_page_length=0,
	)
	if not sessions:
		frappe.throw(_("This Term has no active weekly classes to export."))

	course_fields = [
		"name", "course_name", "course_name_zh", "status", "min_age", "max_age", "duration_mins",
		"total_session_per_term", "full_term_fee", "is_makeup_course",
	]
	# Use the same precedence as billing; a configured zero must stay zero.
	from qas_custom.modules.billing.commands import get_trial_class_fee_field
	trial_field = get_trial_class_fee_field()
	if trial_field:
		course_fields.append(trial_field)
	courses = frappe.get_all("Course", fields=course_fields, limit_page_length=0)
	teachers = frappe.get_all("Teacher", fields=["name", "teacher_name", "status"], limit_page_length=0)
	rooms = frappe.get_all("Classroom", fields=["name", "classroom_name", "campus"], limit_page_length=0)
	campuses = frappe.get_all("Campus", fields=["name", "campus_name"], limit_page_length=0)
	try:
		content = build_timetable_zip(sessions, courses, teachers, rooms, campuses, trial_field)
	except ValueError as error:
		frappe.throw(str(error))
	label = frappe.db.get_value("Term", term, "term_name") or term
	filename = re.sub(r"[^\w.-]+", "_", str(label)).strip("._") or "term"
	frappe.local.response.filename = f"{filename}_timetable.zip"
	frappe.local.response.filecontent = content
	frappe.local.response.content_type = "application/zip"
	frappe.local.response.display_content_as = "attachment"
	frappe.local.response.type = "download"


def build_timetable_zip(sessions, courses, teachers, rooms, campuses, trial_field=None):
	course_map = {row["name"]: row for row in courses}
	teacher_map = {row["name"]: row for row in teachers}
	room_map = {row["name"]: row for row in rooms}
	campus_map = {row["name"]: row for row in campuses}
	session_rows = []
	used_courses = set()
	used_teachers = set()
	for session in sessions:
		label = session.get("name", "Weekly class")
		course = _reference(course_map, session.get("course"), label, "course")
		if cint(course.get("is_makeup_course")):
			continue
		campus = _reference(campus_map, session.get("campus"), label, "campus")
		campus_key = re.sub(r"[^a-z]", "", campus["campus_name"].lower())
		if campus_key not in CAMPUS_KEYS:
			raise ValueError(f"{label}: campus {campus['campus_name']} is not supported by the timetable planner.")
		room = _reference(room_map, session.get("classroom"), label, "classroom")
		if room.get("campus") != session.get("campus") or not room.get("classroom_name"):
			raise ValueError(f"{label}: check the classroom name and campus before exporting.")
		teacher_name = ""
		if session.get("teacher"):
			teacher = _reference(teacher_map, session["teacher"], label, "teacher")
			teacher_name = _teacher_name(teacher)
			used_teachers.add(teacher["name"])
		weekday = WEEKDAYS.get(session.get("day_of_week"))
		if not weekday:
			raise ValueError(f"{label}: choose a valid weekday before exporting.")
		language = session.get("class_language") or "English"
		if language not in ("English", "Chinese"):
			raise ValueError(f"{label}: language {language} is not supported by the timetable planner.")
		used_courses.add(course["name"])
		session_rows.append({
			"course_id": course["name"], "language": language, "teacher_name": teacher_name,
			"room": _room_name(room["classroom_name"], label), "start_time": _start_time(session.get("start_time"), label),
			"campus": CAMPUS_KEYS[campus_key], "weekday": weekday,
		})
	if not session_rows:
		raise ValueError("This Term has no active weekly classes to export after excluding dedicated makeup courses.")
	day_order = {day: index for index, day in enumerate(WEEKDAYS.values())}
	session_rows.sort(key=lambda row: (row["campus"], day_order[row["weekday"]], row["start_time"], row["room"], row["course_id"]))
	course_rows = [
		_course_row(course, trial_field) for course in sorted(courses, key=lambda row: row["name"])
		if not cint(course.get("is_makeup_course"))
		and (course.get("status") == "Active" or course["name"] in used_courses)
	]
	names = sorted({
		_teacher_name(teacher) for teacher in teachers
		if teacher.get("status") == "Active" or teacher["name"] in used_teachers
	}, key=lambda name: (name.casefold(), name))
	output = BytesIO()
	with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
		archive.writestr("qas_course_true_meta.csv", _csv_bytes(COURSE_HEADERS, course_rows))
		archive.writestr("qas_session_table.csv", _csv_bytes(SESSION_HEADERS, session_rows))
		archive.writestr("qas_teachers.csv", _csv_bytes(("teacher_name",), [{"teacher_name": name} for name in names]))
	return output.getvalue()


def _reference(records, key, label, field):
	if not key or key not in records:
		raise ValueError(f"{label}: missing {field} {key or ''}. Please fix it before exporting.")
	return records[key]


def _room_name(value, label):
	match = re.fullmatch(r"(?:r|room)\s*([1-5])", value.strip(), re.IGNORECASE)
	if not match:
		raise ValueError(f"{label}: classroom {value} is not supported by the timetable planner (R1–R5).")
	return f"R{match[1]}"


def _teacher_name(teacher):
	name = teacher.get("teacher_name")
	if not name or not name.strip():
		raise ValueError(f"Teacher {teacher['name']}: enter a display name before exporting.")
	return name


def _start_time(value, label):
	if isinstance(value, timedelta):
		seconds = value.total_seconds()
	elif isinstance(value, time):
		seconds = value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1e6
	else:
		try:
			parsed = time.fromisoformat(str(value).zfill(5) if len(str(value)) <= 5 else str(value).zfill(8))
			seconds = parsed.hour * 3600 + parsed.minute * 60 + parsed.second + parsed.microsecond / 1e6
		except ValueError:
			raise ValueError(f"{label}: enter a valid start time before exporting.") from None
	if not 0 <= seconds < 86400 or seconds % 60:
		raise ValueError(f"{label}: start time must be a whole minute between 00:00 and 23:59.")
	return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}"


def _course_row(course, trial_field):
	minutes = course.get("duration_mins")
	if not minutes or float(minutes) <= 0:
		raise ValueError(f"Course {course['name']}: enter a positive lesson duration before exporting.")
	hours = float(minutes) / 60
	count = course.get("total_session_per_term")
	name_en = course.get("course_name") or course["name"]
	name_zh = course.get("course_name_zh") or ""
	min_age, max_age = course.get("min_age"), course.get("max_age")
	if min_age and max_age:
		age_en, age_zh = f"Ages {min_age:g} to {max_age:g} years", f"{min_age:g}-{max_age:g}岁"
	elif min_age:
		age_en, age_zh = f"Ages {min_age:g}+ years", f"{min_age:g}岁以上"
	elif max_age:
		age_en, age_zh = f"Up to {max_age:g} years", f"{max_age:g}岁及以下"
	else:
		age_en = age_zh = ""
	return dict(zip(COURSE_HEADERS, (
		course["name"], " / ".join(filter(None, [name_en, name_zh])), name_en, name_zh,
		age_en, age_zh, f"{hours:g}", minutes, count if count is not None else "",
		f"{hours * count:g}" if count is not None else "",
		f"{hours:g} {'hour' if hours == 1 else 'hours'} per session" + (f" • {count} sessions per term" if count else ""),
		course.get(trial_field, "") if trial_field else "", course.get("full_term_fee", ""),
	)))


def _csv_bytes(headers, rows):
	output = StringIO(newline="")
	writer = csv.DictWriter(output, fieldnames=headers)
	writer.writeheader()
	writer.writerows(rows)
	return output.getvalue().encode("utf-8-sig")
