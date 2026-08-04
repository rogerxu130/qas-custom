from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import add_days, cint, get_time, getdate, now_datetime, today

from qas_custom.modules.course_schedule.queries import get_teacher_name_map, get_weekly_timeslot_map
from qas_custom.modules.attendance.commands import update_attendance_status
from qas_custom.modules.notifications.commands import enqueue_session_staff_notification
from qas_custom.modules.notifications.makeup_parent_notifications import (
	queue_makeup_booking_confirmation,
	queue_makeup_voucher_issued_email,
)
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, DEFAULT_ATTENDANCE_STATUS, create_attendance_entry
from qas_custom.services.display_labels import get_makeup_voucher_label, sync_makeup_voucher_label


MAKEUP_ENROLLMENT_TYPE = "Makeup"
DEFAULT_VOUCHER_EXPIRY_DAYS = 365
LEGACY_VOUCHER_EXPIRY_DAYS = 90
MAKEUP_REDEEMABLE_SESSION_WINDOW_DAYS = 365
REDEEMABLE_EXISTING_ATTENDANCE_STATUSES = {"Cancelled", "Leave"}
DEFAULT_LEAVE_ATTENDANCE_STATUSES = {"To be started"}
MARKED_MAKEUP_ATTENDANCE_STATUSES = {"Present", "Late", "Absent"}


def submit_parent_leave_request_core(
	parent,
	students: list[dict],
	student: str,
	course_session: str,
	*,
	allowed_attendance_statuses=None,
	allow_started_session: bool = False,
	notify_staff: bool = True,
	notify_parent_voucher: bool = True,
	attendance_entry: str | None = None,
):
	if not student:
		frappe.throw("Please select a student.")
	if not course_session:
		frappe.throw("Please select a class session.")

	selected_student = _validate_student_for_parent(student, students)
	session_doc, attendance_row, timeslot = _get_leave_session(
		student=selected_student,
		course_session=course_session,
		allowed_attendance_statuses=allowed_attendance_statuses,
		allow_started_session=allow_started_session,
		attendance_entry=attendance_entry,
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
	leave_request.flags.qas_leave_attendance_entry = attendance_row.name
	# The Leave Request hook normally creates the voucher immediately. Parent
	# completion flows need to decide whether the voucher email belongs to a
	# retained voucher or an immediately-booked makeup, so process it explicitly.
	leave_request.flags.qas_skip_makeup_processing = True
	leave_request.insert()
	leave_request.reload()
	result = process_leave_request(
		leave_request,
		attendance_entry=attendance_row.name,
		notify_parent_voucher=notify_parent_voucher,
	)
	voucher_id = result.get("makeup_voucher") or _get_voucher_for_leave_request(leave_request)
	voucher_label = sync_makeup_voucher_label(voucher_id)
	notification = _queue_leave_requested_notification(
		notify_staff=notify_staff,
		course_session=session_doc.name,
		student=selected_student,
		leave_request=leave_request.name,
	)

	return {
		"leave_request": leave_request.name,
		"makeup_voucher": voucher_id,
		"makeup_voucher_label": voucher_label or voucher_id,
		"session": _build_leave_session_payload(session_doc, timeslot, selected_student, "Leave"),
		"notification": notification,
	}


def _queue_leave_requested_notification(*, notify_staff: bool, course_session: str, student: str, leave_request: str):
	if not notify_staff:
		return {"skipped": True, "reason": "retroactive_school_admin_leave"}
	return enqueue_session_staff_notification(
		"leave_requested",
		course_session=course_session,
		student=student,
		source_doctype="Leave Request",
		source_document=leave_request,
	)


def process_leave_request_after_insert(doc, method=None):
	if getattr(doc, "flags", None) and doc.flags.get("qas_skip_makeup_processing"):
		return {"skipped": True, "reason": "processed_by_parent_completion_flow"}
	attendance_entry = doc.flags.get("qas_leave_attendance_entry") if getattr(doc, "flags", None) else None
	return process_leave_request(doc, attendance_entry=attendance_entry)


def process_leave_request(leave_request, attendance_entry=None, *, notify_parent_voucher: bool = True):
	doc = frappe.get_doc("Leave Request", leave_request) if isinstance(leave_request, str) else leave_request
	if not doc or doc.get("status") != "Approved":
		return {"skipped": True, "reason": "Leave request is not approved."}
	if not doc.get("student") or not doc.get("course_session"):
		frappe.throw("Leave request requires a student and course session.")

	_populate_leave_request_context(doc)
	attendance_entry = _mark_leave_attendance(doc, attendance_entry=attendance_entry)
	voucher = _ensure_leave_makeup_voucher(doc, notify_parent=notify_parent_voucher)
	_set_leave_request_voucher(doc.name, voucher.name)
	return {
		"leave_request": doc.name,
		"attendance_entry": attendance_entry,
		"makeup_voucher": voucher.name,
	}


def get_parent_leave_makeup_options_core(
	parent,
	students: list[dict],
	student: str,
	course_session: str,
	redeem_student: str | None = None,
	campus: str | None = None,
):
	"""Preview parent-eligible makeup sessions without creating leave or voucher data."""
	selected_student = _validate_student_for_parent(student, students)
	session_doc, _attendance_row, timeslot = _get_leave_session(
		student=selected_student,
		course_session=course_session,
	)
	_validate_no_active_leave(student=selected_student, course_session=session_doc.name)

	preview_voucher = frappe._dict({
		"student": selected_student,
		"course": timeslot.course,
		"original_session": session_doc.name,
	})
	selected_redeem_student = _get_redeem_student(redeem_student, preview_voucher, students)
	all_available_sessions = _get_redeemable_makeup_sessions(
		preview_voucher,
		selected_redeem_student,
		excluded_session_ids={session_doc.name},
	)
	source_campus = str(getattr(timeslot, "campus", None) or "").strip()
	available_campuses = {str(row.get("campus") or "").strip() for row in all_available_sessions if row.get("campus")}
	campuses = ([source_campus] if source_campus else []) + sorted(
		campus_name for campus_name in available_campuses if campus_name != source_campus
	)
	selected_campus = str(campus or "").strip()
	if selected_campus not in campuses:
		selected_campus = source_campus or (campuses[0] if campuses else "")
	available_sessions = [
		row for row in all_available_sessions
		if not selected_campus or str(row.get("campus") or "").strip() == selected_campus
	]
	return {
		"source_session": _build_leave_session_payload(
			session_doc,
			timeslot,
			selected_student,
			"To be started",
		),
		"students": [_build_redeem_student_payload(row) for row in students],
		"selected_redeem_student": selected_redeem_student,
		"source_campus": source_campus,
		"campuses": campuses,
		"selected_campus": selected_campus,
		"available_sessions": available_sessions,
	}


def complete_parent_leave_and_redeem_core(
	parent,
	students: list[dict],
	student: str,
	course_session: str,
	session_id: str,
	redeem_student: str | None = None,
):
	"""Create leave and immediately consume its voucher in one request."""
	if not session_id:
		frappe.throw("Please select a makeup session.")
	if session_id == course_session:
		frappe.throw("The original class cannot be booked as its own makeup session.")
	selected_source_student = _validate_student_for_parent(student, students)
	existing_leave, existing_voucher = _get_active_leave_and_voucher(
		student=selected_source_student,
		course_session=course_session,
	)
	if existing_leave and existing_voucher:
		if (
			existing_voucher.get("status") == "Used"
			and existing_voucher.get("used_on_session") == session_id
		):
			selected_redeem_student = _get_redeem_student(redeem_student, existing_voucher, students)
			booking_result = redeem_parent_voucher_core(
				parent=parent,
				students=students,
				voucher_id=existing_voucher.name,
				session_id=session_id,
				student=selected_redeem_student,
			)
			return {
				"leave_request": existing_leave.get("name"),
				"voucher": booking_result["voucher"],
				"attendance_entry": booking_result["attendance_entry"],
				"session": booking_result["session"],
				"leave_notification": {
					"queued": False,
					"skipped": True,
					"duplicate": True,
				},
				"booking_notification": booking_result["notification"],
				"parent_notification": booking_result["parent_notification"],
				"booking_created": False,
				"duplicate": True,
			}
		frappe.throw("This leave has already been confirmed. Refresh your vouchers before choosing another makeup class.")

	options = get_parent_leave_makeup_options_core(
		parent=parent,
		students=students,
		student=selected_source_student,
		course_session=course_session,
		redeem_student=redeem_student,
	)
	selected_redeem_student = options["selected_redeem_student"]
	if session_id not in {row.get("session_id") for row in options["available_sessions"]}:
		frappe.throw("This class session is not available for this makeup booking.")

	leave_result = submit_parent_leave_request_core(
		parent=parent,
		students=students,
		student=selected_source_student,
		course_session=course_session,
		notify_staff=False,
		notify_parent_voucher=False,
	)
	booking_result = redeem_parent_voucher_core(
		parent=parent,
		students=students,
		voucher_id=leave_result["makeup_voucher"],
		session_id=session_id,
		student=selected_redeem_student,
	)
	leave_notification = _queue_leave_requested_notification(
		notify_staff=True,
		course_session=course_session,
		student=student,
		leave_request=leave_result["leave_request"],
	)
	return {
		"leave_request": leave_result["leave_request"],
		"voucher": booking_result["voucher"],
		"attendance_entry": booking_result["attendance_entry"],
		"source_session": leave_result["session"],
		"session": booking_result["session"],
		"leave_notification": leave_notification,
		"booking_notification": booking_result["notification"],
		"parent_notification": booking_result["parent_notification"],
		"booking_created": booking_result["booking_created"],
	}


def complete_parent_leave_and_keep_voucher_core(
	parent,
	students: list[dict],
	student: str,
	course_session: str,
):
	"""Create leave only after the parent explicitly chooses to retain a voucher."""
	selected_source_student = _validate_student_for_parent(student, students)
	existing_leave, existing_voucher = _get_active_leave_and_voucher(
		student=selected_source_student,
		course_session=course_session,
	)
	if existing_leave and existing_voucher:
		if existing_voucher.get("status") == "Valid":
			return {
				"leave_request": existing_leave.get("name"),
				"makeup_voucher": existing_voucher.name,
				"makeup_voucher_label": get_makeup_voucher_label(existing_voucher),
				"notification": {
					"queued": False,
					"skipped": True,
					"duplicate": True,
				},
				"duplicate": True,
			}
		frappe.throw("This leave has already been confirmed and its makeup voucher has been used.")

	return submit_parent_leave_request_core(
		parent=parent,
		students=students,
		student=selected_source_student,
		course_session=course_session,
		notify_staff=True,
		notify_parent_voucher=True,
	)


def cancel_parent_leave_request_core(parent, students: list[dict], voucher_id: str | None):
	voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
	_validate_voucher_available_for_cancel(voucher)

	restore_status = _restore_leave_attendance(voucher)
	voucher.status = "Cancelled"
	voucher.save(ignore_permissions=True)

	if voucher.get("leave_request") and frappe.db.exists("Leave Request", voucher.leave_request):
		frappe.db.set_value("Leave Request", voucher.leave_request, "status", "Cancelled", update_modified=True)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"attendance_status": restore_status,
	}


def get_parent_redeemable_sessions_core(
	parent,
	students: list[dict],
	voucher_id: str | None,
	student: str | None = None,
	*,
	allow_ordinary_cross_course: bool = False,
):
	voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
	_validate_voucher_available_for_redeem(voucher)
	selected_student = _get_redeem_student(student, voucher, students)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"students": [_build_redeem_student_payload(row) for row in students],
		"selected_student": selected_student,
		"available_sessions": _get_redeemable_makeup_sessions(
			voucher,
			selected_student,
			allow_ordinary_cross_course=allow_ordinary_cross_course,
			excluded_session_ids={voucher.get("original_session")} if voucher.get("original_session") else None,
		),
	}


def redeem_parent_voucher_core(
	parent,
	students: list[dict],
	voucher_id: str | None,
	session_id: str | None,
	student: str | None = None,
	*,
	allow_ordinary_cross_course: bool = False,
	notify_staff: bool = True,
	notify_parent: bool = True,
):
	if not session_id:
		frappe.throw("Please select a makeup session.")

	voucher = _get_parent_makeup_voucher(voucher_id, parent.name)
	selected_student = _get_redeem_student(student, voucher, students)

	if voucher.get("status") == "Used" and voucher.get("used_on_session") == session_id:
		used_student = _get_voucher_used_by_student(voucher) or selected_student
		existing = _get_attendance_entry_used_by_voucher(voucher, session_id, used_student)
		attendance_entry = existing.get("name") if existing else create_makeup_attendance_entry(
			voucher=voucher,
			session_id=session_id,
			student=used_student,
		)
		notification = _queue_makeup_notification(
			event="makeup_booked",
			notify_staff=notify_staff,
			course_session=session_id,
			student=used_student,
			voucher=voucher.name,
		)
		return {
			"voucher": _build_makeup_voucher_payload(voucher),
			"attendance_entry": attendance_entry,
			"session": _build_redeem_session_payload(session_id),
			"notification": notification,
			"parent_notification": {
				"queued": False,
				"skipped": True,
				"duplicate": True,
				"reason": "The makeup booking already exists.",
			},
			"booking_created": False,
		}

	_validate_voucher_available_for_redeem(voucher)
	_validate_session_can_redeem_voucher(
		voucher,
		session_id,
		selected_student,
		allow_ordinary_cross_course=allow_ordinary_cross_course,
	)

	attendance_entry = redeem_voucher_attendance_entry(
		voucher=voucher,
		session_id=session_id,
		student=selected_student,
	)

	voucher.status = "Used"
	voucher.used_on_session = session_id
	voucher.used_date = today()
	_set_voucher_used_by_student(voucher, selected_student)
	if frappe.db.has_column("Makeup Voucher", "voucher_label"):
		voucher.voucher_label = get_makeup_voucher_label({**voucher.as_dict(), "voucher_label": None})
	voucher.flags.skip_makeup_attendance_sync = True
	voucher.save(ignore_permissions=True)
	notification = _queue_makeup_notification(
		event="makeup_booked",
		notify_staff=notify_staff,
		course_session=session_id,
		student=selected_student,
		voucher=voucher.name,
	)
	parent_notification = (
		queue_makeup_booking_confirmation(voucher.name, session_id, selected_student)
		if notify_parent
		else {
			"queued": False,
			"skipped": True,
			"reason": "deferred_until_makeup_transaction_completes",
		}
	)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"attendance_entry": attendance_entry,
		"session": _build_redeem_session_payload(session_id),
		"notification": notification,
		"parent_notification": parent_notification,
		"booking_created": True,
	}


def cancel_makeup_booking_core(voucher, *, confirm_cancel=False):
	if voucher.get("status") != "Used":
		frappe.throw("Only a used makeup voucher can have its booking cancelled.")

	session_id = voucher.get("used_on_session")
	if not session_id:
		frappe.throw("This used makeup voucher is missing its booked session.")
	if not cint(confirm_cancel):
		frappe.throw("Confirm cancellation of this makeup booking before continuing.")
	student = _get_voucher_used_by_student(voucher) or voucher.get("student")
	if not student:
		frappe.throw("This used makeup voucher is missing its booked student.")

	attendance = _get_makeup_booking_attendance(voucher, session_id, student)
	attendance_status_before = attendance.get("status") or ""
	session = _build_redeem_session_payload(session_id)

	attendance.status = "Cancelled"
	attendance.save(ignore_permissions=True)

	voucher.status = "Valid"
	voucher.used_on_session = None
	voucher.used_date = None
	_set_voucher_used_by_student(voucher, None)
	voucher.flags.skip_makeup_attendance_sync = True
	voucher.save(ignore_permissions=True)

	return {
		"voucher": _build_makeup_voucher_payload(voucher),
		"attendance": {
			"attendance_entry": attendance.name,
			"status_before": attendance_status_before,
			"status": attendance.get("status"),
			"requires_marked_attendance_override": attendance_status_before in MARKED_MAKEUP_ATTENDANCE_STATUSES,
		},
		"student": student,
		"session": session,
	}


def _get_makeup_booking_attendance(voucher, session_id: str, student: str):
	voucher_name = voucher if isinstance(voucher, str) else voucher.name
	rows_by_name = {}
	for row in frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={"source_doctype": "Makeup Voucher", "source_document": voucher_name},
		fields=["name", "course_session", "student", "status"],
		limit_page_length=0,
	):
		rows_by_name[row.get("name")] = row

	if frappe.db.has_column(ATTENDANCE_DOCTYPE, "makeup_voucher"):
		for row in frappe.get_all(
			ATTENDANCE_DOCTYPE,
			filters={"makeup_voucher": voucher_name},
			fields=["name", "course_session", "student", "status"],
			limit_page_length=0,
		):
			rows_by_name[row.get("name")] = row

	matches = [
		row for row in rows_by_name.values()
		if row.get("course_session") == session_id and row.get("student") == student
	]
	if len(matches) != 1:
		frappe.throw(
			"The makeup booking attendance could not be identified safely. Refresh the family and review the voucher before trying again."
		)
	return frappe.get_doc(ATTENDANCE_DOCTYPE, matches[0].get("name"))


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


def redeem_voucher_attendance_entry(voucher, session_id: str, student: str):
	reusable_row = _get_reusable_attendance_row_for_voucher(student, session_id)
	if reusable_row:
		return _restore_attendance_row_from_voucher(voucher, reusable_row)

	return create_makeup_attendance_entry(
		voucher=voucher,
		session_id=session_id,
		student=student,
		prevent_student_duplicate=True,
	)


def sync_makeup_voucher_attendance_after_save(doc, method=None):
	if getattr(doc, "flags", None) and doc.flags.get("skip_makeup_attendance_sync"):
		return None
	if doc.get("status") != "Used" or not doc.get("used_on_session"):
		return None
	student = _get_voucher_used_by_student(doc) or doc.get("student")
	if not student:
		return None
	existing = _get_attendance_entry_used_by_voucher(doc, doc.used_on_session, student)
	if existing:
		return existing.get("name")
	return redeem_voucher_attendance_entry(
		voucher=doc,
		session_id=doc.used_on_session,
		student=student,
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


def _validate_voucher_available_for_cancel(voucher):
	if voucher.get("status") != "Valid":
		frappe.throw("Only unused leave vouchers can be cancelled.")
	if voucher.get("used_on_session"):
		frappe.throw("This makeup voucher has already been used.")
	if not voucher.get("original_session"):
		frappe.throw("This makeup voucher is not linked to a leave session.")

	session_doc = frappe.get_doc("Course Sessions", voucher.original_session)
	if session_doc.get("status") == "Cancelled":
		frappe.throw("The original class session has been cancelled.")
	timeslot = frappe.get_cached_doc("Weekly Timeslot", session_doc.weekly_timeslot) if session_doc.weekly_timeslot else None
	if not timeslot:
		frappe.throw("The original class session is missing a weekly timeslot.")
	if _get_session_start(session_doc, timeslot) <= now_datetime():
		frappe.throw("This leave can no longer be cancelled because the class has already started.")


def _get_redeemable_makeup_sessions(
	voucher,
	student: str | None = None,
	*,
	allow_ordinary_cross_course: bool = False,
	excluded_session_ids=None,
):
	redeem_student = student or voucher.student
	excluded_session_ids = {value for value in (excluded_session_ids or set()) if value}
	session_rows = frappe.get_all(
		"Course Sessions",
		filters={
			"session_date": [
				"between",
				[getdate(today()), getdate(add_days(today(), MAKEUP_REDEEMABLE_SESSION_WINDOW_DAYS))],
			],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, modified asc",
		limit=0,
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
		if session.get("name") in excluded_session_ids:
			continue
		timeslot = timeslot_map.get(session.get("weekly_timeslot"))
		if not timeslot or not _course_accepts_makeup_voucher(
			timeslot.get("course"),
			voucher.get("course"),
			allow_ordinary_cross_course=allow_ordinary_cross_course,
		):
			continue
		if not _student_session_can_redeem_voucher(redeem_student, session["name"]):
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


def _validate_session_can_redeem_voucher(
	voucher,
	session_id: str,
	student: str,
	*,
	allow_ordinary_cross_course: bool = False,
):
	available_session_ids = {
		row["session_id"]
		for row in _get_redeemable_makeup_sessions(
			voucher,
			student,
			allow_ordinary_cross_course=allow_ordinary_cross_course,
			excluded_session_ids={voucher.get("original_session")} if voucher.get("original_session") else None,
		)
	}
	if session_id not in available_session_ids:
		frappe.throw("This class session is not available for this makeup voucher.")


def _student_session_can_redeem_voucher(student: str | None, session_id: str):
	if not student or not session_id:
		return False
	rows = _get_student_session_attendance_rows(student, session_id)
	if not rows:
		return True
	return all((row.get("status") or "") in REDEEMABLE_EXISTING_ATTENDANCE_STATUSES for row in rows)


def _get_reusable_attendance_row_for_voucher(student: str, session_id: str):
	rows = _get_student_session_attendance_rows(student, session_id)
	if not rows:
		return None
	if not all((row.get("status") or "") in REDEEMABLE_EXISTING_ATTENDANCE_STATUSES for row in rows):
		return None

	def priority(row):
		status_priority = 0 if row.get("status") == "Leave" else 1
		source_priority = 0 if row.get("source_doctype") == "Enrollment" else 1
		return (status_priority, source_priority, str(row.get("creation") or ""))

	return sorted(rows, key=priority)[0]


def _get_student_session_attendance_rows(student: str, session_id: str):
	return frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={
			"course_session": session_id,
			"student": student,
		},
		fields=[
			"name",
			"course_session",
			"student",
			"enrollment_type",
			"status",
			"source_doctype",
			"source_document",
			"makeup_voucher",
			"creation",
		],
		order_by="creation asc",
	)


def _restore_attendance_row_from_voucher(voucher, attendance_row):
	label = get_makeup_voucher_label(voucher)
	result = update_attendance_status(
		course_session=attendance_row.get("course_session"),
		attendance_row=attendance_row.get("name"),
		status=DEFAULT_ATTENDANCE_STATUS,
		actor=frappe.session.user,
		comment=f"Restored from Makeup Voucher {label}",
	)
	attendance_entry = result.get("attendance_entry") or attendance_row.get("name")
	if frappe.db.has_column(ATTENDANCE_DOCTYPE, "makeup_voucher"):
		frappe.db.set_value(
			ATTENDANCE_DOCTYPE,
			attendance_entry,
			"makeup_voucher",
			voucher.name,
			update_modified=True,
		)
	return attendance_entry


def _get_attendance_entry_used_by_voucher(voucher, session_id: str, student: str | None = None):
	voucher_name = voucher if isinstance(voucher, str) else voucher.name
	base_filters = {"course_session": session_id}
	if student:
		base_filters["student"] = student

	source_rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={
			**base_filters,
			"source_doctype": "Makeup Voucher",
			"source_document": voucher_name,
		},
		fields=["name"],
		order_by="modified desc",
		limit=1,
	)
	if source_rows:
		return source_rows[0]

	if not frappe.db.has_column(ATTENDANCE_DOCTYPE, "makeup_voucher"):
		return None
	voucher_rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters={
			**base_filters,
			"makeup_voucher": voucher_name,
		},
		fields=["name"],
		order_by="modified desc",
		limit=1,
	)
	return voucher_rows[0] if voucher_rows else None


def _course_accepts_makeup_voucher(
	target_course: str | None,
	voucher_course: str | None,
	*,
	allow_ordinary_cross_course: bool = False,
):
	if not target_course:
		return False
	if target_course == voucher_course:
		return True

	try:
		course_doc = frappe.get_cached_doc("Course", target_course)
	except frappe.DoesNotExistError:
		return False

	if not course_doc.get("is_makeup_course"):
		return allow_ordinary_cross_course

	accepted_courses = [
		row.get("course")
		for row in course_doc.get("accepted_makeup_course") or []
		if row.get("course")
	]
	return voucher_course in accepted_courses


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
		"price_difference_invoice": voucher.get("price_difference_invoice"),
	}


def _queue_makeup_notification(*, event: str, notify_staff: bool, course_session: str, student: str, voucher: str):
	if not notify_staff:
		return {"skipped": True, "reason": "deferred_until_makeup_transaction_completes"}
	return enqueue_session_staff_notification(
		event,
		course_session=course_session,
		student=student,
		source_doctype="Makeup Voucher",
		source_document=voucher,
	)


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


def _set_voucher_used_by_student(voucher, student: str | None):
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


def _build_leave_session_payload(session_doc, timeslot, student: str, attendance_status: str):
	return {
		"session_id": session_doc.name,
		"student": student,
		"course": timeslot.course,
		"session_date": session_doc.session_date,
		"day_of_week": timeslot.day_of_week,
		"start_time": timeslot.start_time,
		"end_time": timeslot.end_time,
		"campus": timeslot.campus,
		"classroom": timeslot.classroom,
		"attendance_status": attendance_status,
	}


def _get_leave_session(
	student: str,
	course_session: str,
	*,
	allowed_attendance_statuses=None,
	allow_started_session: bool = False,
	attendance_entry: str | None = None,
):
	session_doc = frappe.get_doc("Course Sessions", course_session)
	allowed_statuses = set(allowed_attendance_statuses or DEFAULT_LEAVE_ATTENDANCE_STATUSES)
	attendance_filters = {"course_session": course_session, "student": student}
	if attendance_entry:
		attendance_filters["name"] = attendance_entry
	attendance_row = frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		attendance_filters,
		["name", "status"],
		as_dict=True,
	)
	if not attendance_row:
		frappe.throw("This student is not listed in the selected class session.", frappe.PermissionError)

	if attendance_row.status not in allowed_statuses:
		frappe.throw("This class session is not available for leave.")

	if not session_doc.weekly_timeslot:
		frappe.throw("The selected class session is missing a weekly timeslot.")

	timeslot = frappe.get_cached_doc("Weekly Timeslot", session_doc.weekly_timeslot)
	if not timeslot.course:
		frappe.throw("The selected class session is missing a course.")

	if not allow_started_session and _get_session_start(session_doc, timeslot) <= now_datetime():
		frappe.throw("This class has already started.")

	return session_doc, attendance_row, timeslot


def _get_session_start(session_doc, timeslot):
	if not session_doc.session_date or not timeslot.start_time:
		frappe.throw("The selected class session is missing date or time.")

	return datetime.combine(
		getdate(session_doc.session_date),
		get_time(timeslot.start_time),
	)


def _populate_leave_request_context(doc):
	session_doc = frappe.get_doc("Course Sessions", doc.course_session)
	updates = {}
	if session_doc.get("weekly_timeslot") and not doc.get("weekly_timeslot"):
		updates["weekly_timeslot"] = session_doc.weekly_timeslot
	if session_doc.get("session_date") and not doc.get("session_date"):
		updates["session_date"] = session_doc.session_date
	if session_doc.get("weekly_timeslot") and not doc.get("course"):
		course = frappe.db.get_value("Weekly Timeslot", session_doc.weekly_timeslot, "course")
		if course:
			updates["course"] = course
	for fieldname, value in updates.items():
		doc.set(fieldname, value)
	if updates and doc.get("name"):
		frappe.db.set_value("Leave Request", doc.name, updates, update_modified=False)
	return doc


def _mark_leave_attendance(doc, attendance_entry=None):
	if attendance_entry:
		attendance_entry = frappe.db.get_value(
			ATTENDANCE_DOCTYPE,
			{
				"name": attendance_entry,
				"course_session": doc.course_session,
				"student": doc.student,
				"status": ["!=", "Cancelled"],
			},
			"name",
		)
	else:
		attendance_entry = frappe.db.get_value(
			ATTENDANCE_DOCTYPE,
			{
				"course_session": doc.course_session,
				"student": doc.student,
				"status": ["!=", "Cancelled"],
			},
			"name",
			order_by="creation asc",
		)
	if not attendance_entry:
		frappe.throw("This student is not listed in the selected class session.")

	row = frappe.get_doc(ATTENDANCE_DOCTYPE, attendance_entry)
	if row.get("status") == "Leave":
		return row.name

	result = update_attendance_status(
		course_session=row.course_session,
		attendance_row=row.name,
		status="Leave",
		actor=frappe.session.user,
		comment=f"Leave request {doc.name}",
	)
	return result.get("attendance_entry") or row.name


def _ensure_leave_makeup_voucher(doc, *, notify_parent: bool = True):
	existing = frappe.db.exists("Makeup Voucher", {"leave_request": doc.name})
	if not existing and doc.get("makeup_voucher") and frappe.db.exists("Makeup Voucher", doc.get("makeup_voucher")):
		existing = doc.get("makeup_voucher")
	if not existing:
		existing = frappe.db.exists(
			"Makeup Voucher",
			{
				"student": doc.student,
				"original_session": doc.course_session,
				"status": ["in", ["Valid", "Used"]],
			},
		)
	if existing:
		voucher = frappe.get_doc("Makeup Voucher", existing)
		changed = False
		for fieldname, value in {
			"leave_request": doc.name,
			"course": doc.get("course"),
			"original_session": doc.course_session,
		}.items():
			if value and voucher.meta.has_field(fieldname) and not voucher.get(fieldname):
				voucher.set(fieldname, value)
				changed = True
		if changed:
			voucher.save(ignore_permissions=True)
		return voucher

	voucher = frappe.new_doc("Makeup Voucher")
	voucher.student = doc.student
	voucher.course = doc.get("course")
	voucher.original_session = doc.course_session
	voucher.leave_request = doc.name
	voucher.status = "Valid"
	voucher.issue_date = today()
	if voucher.meta.has_field("expiry_date"):
		voucher.expiry_date = add_days(today(), DEFAULT_VOUCHER_EXPIRY_DAYS)
	voucher.insert(ignore_permissions=True)
	sync_makeup_voucher_label(voucher.name)
	if notify_parent:
		queue_makeup_voucher_issued_email(voucher.name)
	return frappe.get_doc("Makeup Voucher", voucher.name)


def _set_leave_request_voucher(leave_request, voucher):
	if not leave_request or not voucher:
		return
	if frappe.db.has_column("Leave Request", "makeup_voucher"):
		frappe.db.set_value("Leave Request", leave_request, "makeup_voucher", voucher, update_modified=False)


def _restore_leave_attendance(voucher):
	attendance_entry = frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		{
			"course_session": voucher.original_session,
			"student": voucher.student,
			"status": "Leave",
		},
		"name",
		order_by="modified desc",
	)
	if not attendance_entry:
		return None

	row = frappe.get_doc(ATTENDANCE_DOCTYPE, attendance_entry)
	restore_status = row.get("previous_status") or "To be started"
	if restore_status in {"Leave", "Cancelled"}:
		restore_status = "To be started"
	result = update_attendance_status(
		course_session=row.course_session,
		attendance_row=row.name,
		status=restore_status,
		actor=frappe.session.user,
		comment=f"Leave cancelled from Makeup Voucher {get_makeup_voucher_label(voucher)}",
	)
	return result.get("status")


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


def _get_active_leave_and_voucher(student: str, course_session: str):
	"""Return an existing active leave and its voucher for safe final-action retries."""
	leave_request = frappe.db.get_value(
		"Leave Request",
		{
			"student": student,
			"course_session": course_session,
			"status": "Approved",
		},
		["name", "makeup_voucher"],
		as_dict=True,
	)
	if not leave_request:
		return None, None

	voucher_id = leave_request.get("makeup_voucher") or frappe.db.exists(
		"Makeup Voucher",
		{
			"student": student,
			"original_session": course_session,
			"status": ["in", ["Valid", "Used"]],
		},
	)
	if not voucher_id:
		return leave_request, None
	return leave_request, frappe.get_doc("Makeup Voucher", voucher_id)


def _get_voucher_for_leave_request(leave_request):
	voucher_id = frappe.db.exists("Makeup Voucher", {"leave_request": leave_request.name})
	if voucher_id:
		return voucher_id
	legacy_value = leave_request.get("makeup_voucher")
	if legacy_value and frappe.db.exists("Makeup Voucher", legacy_value):
		return legacy_value
	return legacy_value or None
