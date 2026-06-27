from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import add_days, get_time, getdate, now_datetime, today

from qas_custom.modules.course_schedule.queries import get_teacher_name_map, get_weekly_timeslot_map
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, create_attendance_entry
from qas_custom.services.display_labels import get_makeup_voucher_label, sync_makeup_voucher_label


MAKEUP_ENROLLMENT_TYPE = "Makeup"


def submit_parent_leave_request_core(parent, students: list[dict], student: str, course_session: str):
	if not student:
		frappe.throw("Please select a student.")
	if not course_session:
		frappe.throw("Please select a class session.")

	selected_student = _validate_student_for_parent(student, students)
	session_doc, attendance_row, timeslot = _get_leave_session(
		student=selected_student,
		course_session=course_session,
	)
	_validate_no_active_leave(student=selected_student, course_session=session_doc.name)

	leave_request = frappe.new_doc("Leave Request")
	leave_request.student = selected_student
	leave_request.course_session = session_doc.name
	leave_request.weekly_timeslot = session_doc.weekly_timeslot
	leave_request.course = timeslot.course
	leave_request.session_date = session_doc.session_date
	leave_request.status = "Approved"
	leave_request.flags.ignore_permissions = True
	leave_request.insert()
	leave_request.reload()
	voucher_label = sync_makeup_voucher_label(leave_request.get("makeup_voucher"))

	return {
		"leave_request": leave_request.name,
		"makeup_voucher": leave_request.get("makeup_voucher"),
		"makeup_voucher_label": voucher_label or leave_request.get("makeup_voucher"),
		"session": {
			"session_id": session_doc.name,
			"student": selected_student,
			"course": timeslot.course,
			"session_date": session_doc.session_date,
			"day_of_week": timeslot.day_of_week,
			"start_time": timeslot.start_time,
			"end_time": timeslot.end_time,
			"campus": timeslot.campus,
			"classroom": timeslot.classroom,
			"attendance_status": "Leave",
		},
	}


def get_parent_redeemable_sessions_core(parent, students: list[dict], voucher_id: str | None, student: str | None = None):
	voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
	_validate_voucher_available_for_redeem(voucher)
	selected_student = _get_redeem_student(student, voucher, students)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"students": [_build_redeem_student_payload(row) for row in students],
		"selected_student": selected_student,
		"available_sessions": _get_redeemable_makeup_sessions(voucher, selected_student),
	}


def redeem_parent_voucher_core(
	parent,
	students: list[dict],
	voucher_id: str | None,
	session_id: str | None,
	student: str | None = None,
):
	if not session_id:
		frappe.throw("Please select a makeup session.")

	voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
	selected_student = _get_redeem_student(student, voucher, students)

	if voucher.get("status") == "Used" and voucher.get("used_on_session") == session_id:
		used_student = _get_voucher_used_by_student(voucher) or selected_student
		attendance_entry = create_makeup_attendance_entry(
			voucher=voucher,
			session_id=session_id,
			student=used_student,
		)
		return {
			"voucher": _build_makeup_voucher_payload(voucher),
			"attendance_entry": attendance_entry,
			"session": _build_redeem_session_payload(session_id),
		}

	_validate_voucher_available_for_redeem(voucher)
	_validate_session_can_redeem_voucher(voucher, session_id, selected_student)

	attendance_entry = create_makeup_attendance_entry(
		voucher=voucher,
		session_id=session_id,
		student=selected_student,
		prevent_student_duplicate=True,
	)

	voucher.status = "Used"
	voucher.used_on_session = session_id
	voucher.used_date = today()
	_set_voucher_used_by_student(voucher, selected_student)
	if frappe.db.has_column("Makeup Voucher", "voucher_label"):
		voucher.voucher_label = get_makeup_voucher_label({**voucher.as_dict(), "voucher_label": None})
	voucher.save(ignore_permissions=True)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"attendance_entry": attendance_entry,
		"session": _build_redeem_session_payload(session_id),
	}


def create_makeup_attendance_entry(voucher, session_id: str, student: str, prevent_student_duplicate: bool = False):
	return create_attendance_entry(
		course_session=session_id,
		student=student,
		enrollment_type=MAKEUP_ENROLLMENT_TYPE,
		source_doctype="Makeup Voucher",
		source_document=voucher.name,
		comments=f"Added from Makeup Voucher {get_makeup_voucher_label(voucher)}",
		makeup_voucher=voucher.name,
		prevent_student_duplicate=prevent_student_duplicate,
	)


def _validate_student_for_parent(student: str | None, students: list[dict]) -> str:
	if not student:
		frappe.throw("Please select a student.")

	allowed = {row["name"] for row in students}
	if student not in allowed:
		frappe.throw("This student is not linked to the current parent account.", frappe.PermissionError)
	return student


def _get_parent_makeup_voucher(voucher_id: str | None, parent_name: str):
	if not voucher_id:
		frappe.throw("Makeup voucher is required.")
	if not frappe.db.exists("Makeup Voucher", voucher_id):
		frappe.throw("Makeup voucher was not found.")

	voucher = frappe.get_doc("Makeup Voucher", voucher_id)
	if not voucher.get("student"):
		frappe.throw("This makeup voucher is missing a student.")

	guardian = frappe.db.get_value("Student", voucher.student, "guardian")
	if guardian != parent_name:
		frappe.throw("This makeup voucher is not linked to the current parent account.", frappe.PermissionError)

	return voucher


def _validate_voucher_available_for_redeem(voucher):
	if voucher.get("status") != "Valid":
		frappe.throw("This makeup voucher is not available for booking.")
	if voucher.get("used_on_session"):
		frappe.throw("This makeup voucher has already been used.")
	if voucher.get("expiry_date") and getdate(voucher.expiry_date) < getdate(today()):
		frappe.throw("This makeup voucher has expired.")


def _get_redeemable_makeup_sessions(voucher, student: str | None = None):
	redeem_student = student or voucher.student
	session_rows = frappe.get_all(
		"Course Sessions",
		filters={
			"session_date": ["between", [getdate(today()), getdate(add_days(today(), 90))]],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, modified asc",
		limit=300,
	)
	if not session_rows:
		return []

	timeslot_map = get_weekly_timeslot_map(
		[row["weekly_timeslot"] for row in session_rows if row.get("weekly_timeslot")]
	)
	teacher_map = get_teacher_name_map(
		[row["teacher"] for row in timeslot_map.values() if row.get("teacher")]
	)

	sessions = []
	for session in session_rows:
		if session.get("status") == "Cancelled":
			continue
		timeslot = timeslot_map.get(session.get("weekly_timeslot"))
		if not timeslot or not _course_accepts_makeup_voucher(timeslot.get("course"), voucher.get("course")):
			continue
		if frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{"course_session": session["name"], "student": redeem_student},
		):
			continue

		sessions.append(
			{
				"session_id": session["name"],
				"course": timeslot.get("course"),
				"session_date": session.get("session_date"),
				"day_of_week": timeslot.get("day_of_week"),
				"start_time": timeslot.get("start_time"),
				"end_time": timeslot.get("end_time"),
				"campus": timeslot.get("campus"),
				"classroom": timeslot.get("classroom"),
				"teacher": teacher_map.get(timeslot.get("teacher"), timeslot.get("teacher")),
			}
		)

	return sessions


def _validate_session_can_redeem_voucher(voucher, session_id: str, student: str):
	available_session_ids = {
		row["session_id"] for row in _get_redeemable_makeup_sessions(voucher, student)
	}
	if session_id not in available_session_ids:
		frappe.throw("This class session is not available for this makeup voucher.")


def _course_accepts_makeup_voucher(target_course: str | None, voucher_course: str | None):
	if not target_course:
		return False
	if target_course == voucher_course:
		return True

	try:
		course_doc = frappe.get_cached_doc("Course", target_course)
	except frappe.DoesNotExistError:
		return False

	if not course_doc.get("is_makeup_course"):
		return False

	accepted_courses = [
		row.get("course")
		for row in course_doc.get("accepted_makeup_course") or []
		if row.get("course")
	]
	return not accepted_courses or voucher_course in accepted_courses


def _build_makeup_voucher_payload(voucher):
	voucher_label = get_makeup_voucher_label(voucher)
	return {
		"voucher_id": voucher.name,
		"voucher_label": voucher_label,
		"student": voucher.get("student"),
		"source_student": voucher.get("student"),
		"used_by_student": _get_voucher_used_by_student(voucher),
		"course": voucher.get("course"),
		"status": voucher.get("status"),
		"issue_date": voucher.get("issue_date"),
		"expiry_date": voucher.get("expiry_date"),
		"used_on_session": voucher.get("used_on_session"),
		"used_date": voucher.get("used_date"),
		"leave_request": voucher.get("leave_request"),
	}


def _build_redeem_student_payload(student):
	return {
		"name": student.get("name"),
		"student_name": student.get("student_name"),
		"student_code": student.get("student_code"),
		"student_display": student.get("student_code") or student.get("student_name") or student.get("name"),
		"age": student.get("age") or 0,
		"status": student.get("status"),
	}


def _get_redeem_student(student: str | None, voucher, students) -> str:
	student_names = {row["name"] for row in students}
	preferred_student = student or voucher.student
	if preferred_student not in student_names:
		frappe.throw("This student is not linked to the current parent account.", frappe.PermissionError)
	return preferred_student


def _get_voucher_used_by_student(voucher):
	if frappe.db.has_column("Makeup Voucher", "used_by_student"):
		return voucher.get("used_by_student")
	if frappe.db.has_column("Makeup Voucher", "redeemed_student"):
		return voucher.get("redeemed_student")
	return None


def _set_voucher_used_by_student(voucher, student: str):
	if frappe.db.has_column("Makeup Voucher", "used_by_student"):
		voucher.used_by_student = student
	elif frappe.db.has_column("Makeup Voucher", "redeemed_student"):
		voucher.redeemed_student = student


def _build_redeem_session_payload(session_id: str):
	session = frappe.get_doc("Course Sessions", session_id)
	timeslot = frappe.get_cached_doc("Weekly Timeslot", session.weekly_timeslot) if session.weekly_timeslot else None
	return {
		"session_id": session.name,
		"course": timeslot.course if timeslot else None,
		"session_date": session.session_date,
		"start_time": timeslot.start_time if timeslot else None,
		"end_time": timeslot.end_time if timeslot else None,
		"campus": timeslot.campus if timeslot else None,
		"classroom": timeslot.classroom if timeslot else None,
	}


def _get_leave_session(student: str, course_session: str):
	session_doc = frappe.get_doc("Course Sessions", course_session)
	attendance_row = frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		{"course_session": course_session, "student": student},
		["name", "status"],
		as_dict=True,
	)
	if not attendance_row:
		frappe.throw("This student is not listed in the selected class session.", frappe.PermissionError)

	if attendance_row.status != "To be started":
		frappe.throw("This class session is not available for leave.")

	if not session_doc.weekly_timeslot:
		frappe.throw("The selected class session is missing a weekly timeslot.")

	timeslot = frappe.get_cached_doc("Weekly Timeslot", session_doc.weekly_timeslot)
	if not timeslot.course:
		frappe.throw("The selected class session is missing a course.")

	if _get_session_start(session_doc, timeslot) <= now_datetime():
		frappe.throw("This class has already started.")

	return session_doc, attendance_row, timeslot


def _get_session_start(session_doc, timeslot):
	if not session_doc.session_date or not timeslot.start_time:
		frappe.throw("The selected class session is missing date or time.")

	return datetime.combine(
		getdate(session_doc.session_date),
		get_time(timeslot.start_time),
	)


def _validate_no_active_leave(student: str, course_session: str):
	existing_leave = frappe.db.get_value(
		"Leave Request",
		{
			"student": student,
			"course_session": course_session,
			"status": "Approved",
		},
		["name", "makeup_voucher"],
		as_dict=True,
	)
	if existing_leave:
		frappe.throw("A leave request already exists for this class session.")

	existing_voucher = frappe.db.exists(
		"Makeup Voucher",
		{
			"student": student,
			"original_session": course_session,
			"status": ["in", ["Valid", "Used"]],
		},
	)
	if existing_voucher:
		frappe.throw("A makeup voucher already exists for this class session.")
