from __future__ import annotations

import re
from datetime import datetime

import frappe
from frappe.utils import get_time, getdate


def get_student_display_code(student: str | dict | None):
	doc = _get_student_doc(student)
	if not doc:
		return student if isinstance(student, str) else None

	code = doc.get("student_code") if _has_column("Student", "student_code") else None
	if code:
		return code

	name = doc.get("student_name") or doc.get("name")
	return _student_code_from_name(name)


def get_student_display_name(student: str | dict | None):
	doc = _get_student_doc(student)
	if not doc:
		return student if isinstance(student, str) else None

	code = get_student_display_code(doc)
	name = doc.get("student_name") or doc.get("name")
	if code and code != name:
		return f"{name} ({code})"
	return name


def get_makeup_voucher_label(voucher: str | dict | None):
	doc = _get_voucher_doc(voucher)
	if not doc:
		return voucher if isinstance(voucher, str) else None

	if _has_column("Makeup Voucher", "voucher_label"):
		label = doc.get("voucher_label")
		if label:
			return label

	parts = []
	session_label = _get_session_label(doc.get("original_session"))
	if session_label:
		parts.append(session_label)

	student_code = get_student_display_code(doc.get("student"))
	if student_code:
		parts.append(student_code)

	if doc.get("course"):
		parts.append(doc.get("course"))

	return " · ".join(parts) or doc.get("name")


def sync_makeup_voucher_label(voucher: str | dict | None):
	doc = _get_voucher_doc(voucher)
	if not doc or not _has_column("Makeup Voucher", "voucher_label"):
		return None

	label = get_makeup_voucher_label({**doc, "voucher_label": None})
	if label and label != doc.get("voucher_label"):
		frappe.db.set_value("Makeup Voucher", doc.get("name"), "voucher_label", label, update_modified=False)
	return label


def _get_student_doc(student: str | dict | None):
	if not student:
		return None
	if isinstance(student, dict):
		return student
	if hasattr(student, "as_dict"):
		return student.as_dict()

	fields = ["name", "student_name"]
	if _has_column("Student", "student_code"):
		fields.append("student_code")
	row = frappe.db.get_value("Student", student, fields, as_dict=True)
	return row or {"name": student, "student_name": student}


def _get_voucher_doc(voucher: str | dict | None):
	if not voucher:
		return None
	if isinstance(voucher, dict):
		return voucher
	if hasattr(voucher, "as_dict"):
		return voucher.as_dict()
	if not frappe.db.exists("Makeup Voucher", voucher):
		return None

	fields = ["name", "student", "course", "original_session"]
	for fieldname in ("voucher_label", "leave_request", "issue_date"):
		if _has_column("Makeup Voucher", fieldname):
			fields.append(fieldname)
	return frappe.db.get_value("Makeup Voucher", voucher, fields, as_dict=True)


def _get_session_label(course_session: str | None):
	if not course_session or not frappe.db.exists("Course Sessions", course_session):
		return None

	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["session_date", "weekly_timeslot"],
		as_dict=True,
	)
	if not session:
		return None

	date_label = _format_date(session.get("session_date"))
	time_label = None
	if session.get("weekly_timeslot"):
		start_time = frappe.db.get_value("Weekly Timeslot", session.get("weekly_timeslot"), "start_time")
		time_label = _format_time(start_time)

	return " ".join(part for part in (date_label, time_label) if part)


def _student_code_from_name(name: str | None):
	base = re.sub(r"[^A-Za-z0-9]+", "", (name or "Student").strip().split(" ")[0])
	return base or "Student"


def _format_date(value):
	if not value:
		return None
	return getdate(value).strftime("%d %b %Y").lstrip("0")


def _format_time(value):
	if not value:
		return None
	time_value = get_time(value)
	return datetime.combine(getdate(), time_value).strftime("%I:%M %p").lstrip("0")


def _has_column(doctype: str, fieldname: str):
	return frappe.db.has_column(doctype, fieldname)
