from __future__ import annotations

import frappe
from frappe import _

from frappe.utils import add_days, getdate, now_datetime, today

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.services.class_attendance import get_attendance_entries
from qas_custom.services.inquiry import (
	add_inquiry_note_core,
	build_inquiry_detail,
	build_inquiry_summary,
	mark_inquiry_status_core,
)


def get_campus_admin_me_data():
	profile = _require_campus_admin_profile()
	return {
		"user": frappe.session.user,
		"profile": profile["name"],
		"active": True,
		"campuses": profile["campuses"],
	}


def get_campus_admin_csrf_token_data():
	_require_campus_admin_profile()
	return {
		"csrf_token": frappe.sessions.get_csrf_token(),
	}


def get_campus_admin_dashboard_data(from_date=None, to_date=None):
	profile = _require_campus_admin_profile()
	start_date = getdate(from_date or today())
	end_date = getdate(to_date or add_days(start_date, 3))
	campuses = profile["campuses"]
	return {
		"from_date": str(start_date),
		"to_date": str(end_date),
		"campuses": campuses,
		"trial_lessons": _get_inquiry_dashboard_items(campuses, start_date, end_date, "Trial Lesson"),
		"school_visits": _get_inquiry_dashboard_items(campuses, start_date, end_date, "School Visit"),
		"makeup_bookings": _get_attendance_dashboard_items(campuses, start_date, end_date, "Makeup"),
		"adhoc_bookings": _get_adhoc_booking_dashboard_items(campuses, start_date, end_date),
	}


def get_campus_admin_inquiries_data(status=None, inquiry_type=None, from_date=None, to_date=None, campus=None, queue=None):
	profile = _require_campus_admin_profile()
	campuses = _filter_requested_campus(profile["campuses"], campus)
	filters = {
		"campus": ["in", campuses],
	}
	if status:
		filters["status"] = status
	if inquiry_type:
		filters["inquiry_type"] = inquiry_type
	if from_date and to_date:
		filters["current_appointment_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["current_appointment_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["current_appointment_date"] = ["<=", getdate(to_date)]

	order_by = "current_appointment_date desc, modified desc" if queue == "post_trial" else "current_appointment_date asc, modified desc"
	rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		fields=[
			"name",
			"inquiry_type",
			"status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
		],
		order_by=order_by,
	)
	return {"items": [_build_inquiry_list_item(row) for row in rows]}


def get_campus_admin_inquiry_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return build_inquiry_detail(inquiry)


def add_campus_admin_inquiry_note_data(inquiry=None, note=None):
	_require_inquiry_access(inquiry)
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)


def mark_campus_admin_inquiry_completed_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Completed", actor=frappe.session.user)


def mark_campus_admin_inquiry_no_show_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "No-show", actor=frappe.session.user)


def mark_campus_admin_inquiry_cancelled_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Cancelled", actor=frappe.session.user)


def reopen_campus_admin_inquiry_data(inquiry=None):
	_require_inquiry_access(inquiry)
	if not inquiry:
		frappe.throw(_("Inquiry is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status != "Cancelled":
		frappe.throw(_("Only cancelled inquiries can be reopened."))

	target_status = _get_reopen_status(inquiry_doc)
	previous_status = inquiry_doc.status
	original_course_session = inquiry_doc.course_session
	inquiry_doc.status = target_status
	if target_status == "Needs Review":
		inquiry_doc.review_reason = _("Reopened from cancellation. No original appointment or session was available.")
	else:
		inquiry_doc.review_reason = None
	inquiry_doc.save(ignore_permissions=True)
	_add_system_inquiry_note(
		inquiry_doc,
		_("Inquiry reopened by Campus Admin. Previous status: {0}. Restored status: {1}. Course session kept: {2}.").format(
			previous_status,
			target_status,
			original_course_session or "-",
		),
	)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def mark_campus_admin_inquiry_follow_up_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Follow-up", actor=frappe.session.user)


def get_campus_admin_conversion_sessions_data(inquiry=None, start_date=None, course=None):
	profile = _require_inquiry_access(inquiry)
	inquiry_campus = frappe.db.get_value("Inquiry", inquiry, "campus")
	if inquiry_campus not in profile["campuses"]:
		frappe.throw(_("You do not have access to this inquiry."), frappe.PermissionError)
	return get_conversion_session_options(
		inquiry=inquiry,
		start_date=start_date,
		course=course,
		campus=inquiry_campus,
	)


def convert_campus_admin_inquiry_data(inquiry=None, course_session=None):
	_require_inquiry_access(inquiry)
	_validate_conversion_session_access(inquiry, course_session)
	result = convert_inquiry_to_full_term_core(inquiry, course_session, actor=frappe.session.user)
	return result["inquiry"]


def mark_campus_admin_inquiry_inactive_data(inquiry=None, inactive_reason=None):
	_require_inquiry_access(inquiry)
	return mark_inquiry_inactive_core(inquiry, inactive_reason, actor=frappe.session.user)


def _require_campus_admin_profile():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	rows = frappe.get_all(
		"Campus Admin Profile",
		filters={"user": frappe.session.user, "active": 1},
		fields=["name"],
		limit=1,
	)
	if not rows:
		frappe.throw(_("No active Campus Admin profile is linked to this account."), frappe.PermissionError)

	doc = frappe.get_doc("Campus Admin Profile", rows[0].name)
	campuses = [row.campus for row in doc.get("campuses", []) if row.campus]
	if not campuses:
		frappe.throw(_("Campus Admin profile has no assigned campuses."), frappe.PermissionError)
	return {"name": doc.name, "campuses": campuses}


def _require_inquiry_access(inquiry):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	profile = _require_campus_admin_profile()
	inquiry_campus = frappe.db.get_value("Inquiry", inquiry, "campus")
	if not inquiry_campus:
		frappe.throw(_("Inquiry was not found."))
	if inquiry_campus not in profile["campuses"]:
		frappe.throw(_("You do not have access to this inquiry."), frappe.PermissionError)
	return profile


def _filter_requested_campus(allowed_campuses, requested_campus=None):
	if not requested_campus:
		return allowed_campuses
	if requested_campus not in allowed_campuses:
		frappe.throw(_("You do not have access to the requested campus."), frappe.PermissionError)
	return [requested_campus]


def _validate_conversion_session_access(inquiry, course_session):
	if not course_session:
		frappe.throw(_("Course session is required."))
	profile = _require_inquiry_access(inquiry)
	session = frappe.db.get_value("Course Sessions", course_session, ["weekly_timeslot"], as_dict=True)
	if not session:
		frappe.throw(_("Course session was not found."))
	timeslot = frappe.db.get_value("Weekly Timeslot", session.weekly_timeslot, ["campus"], as_dict=True)
	if not timeslot or timeslot.campus not in profile["campuses"]:
		frappe.throw(_("You do not have access to the selected session."), frappe.PermissionError)


def _get_reopen_status(inquiry_doc):
	if inquiry_doc.course_session or inquiry_doc.current_appointment_date or inquiry_doc.current_appointment_time:
		return "Booked"
	return "Needs Review"


def _add_system_inquiry_note(inquiry_doc, note):
	note_doc = frappe.new_doc("Inquiry Note")
	note_doc.inquiry = inquiry_doc.name
	note_doc.student = inquiry_doc.student
	note_doc.note = note
	note_doc.author = frappe.session.user
	note_doc.edited_at = now_datetime()
	if note_doc.meta.has_field("note_type"):
		note_doc.note_type = "System"
	if note_doc.meta.has_field("source_doctype"):
		note_doc.source_doctype = "Inquiry"
	if note_doc.meta.has_field("source_document"):
		note_doc.source_document = inquiry_doc.name
	note_doc.flags.ignore_permissions = True
	note_doc.insert()


def _get_inquiry_dashboard_items(campuses, start_date, end_date, inquiry_type):
	rows = frappe.get_all(
		"Inquiry",
		filters={
			"campus": ["in", campuses],
			"inquiry_type": inquiry_type,
			"current_appointment_date": ["between", [start_date, end_date]],
			"status": ["in", ["Booked", "Needs Review", "Rescheduled", "No-show"]],
		},
		fields=[
			"name",
			"inquiry_type",
			"status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
		],
		order_by="current_appointment_date asc, current_appointment_time asc",
	)
	student_map = _get_student_map([row.student for row in rows if row.student])
	note_map = _get_latest_note_map([row.name for row in rows])
	return [
		_build_inquiry_dashboard_item(row, student_map.get(row.student), note_map.get(row.name))
		for row in rows
	]


def _get_attendance_dashboard_items(campuses, start_date, end_date, enrollment_type):
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"campus": ["in", campuses]},
		fields=["name", "course", "campus", "classroom", "start_time", "end_time"],
	)
	if not timeslots:
		return []
	timeslot_map = {row.name: row for row in timeslots}
	sessions = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": ["in", list(timeslot_map.keys())],
			"session_date": ["between", [start_date, end_date]],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, modified asc",
	)
	if not sessions:
		return []
	session_map = {row.name: row for row in sessions}
	attendance_rows = get_attendance_entries(
		list(session_map.keys()),
		fields=["name", "course_session", "student", "enrollment_type", "status", "comments", "makeup_voucher"],
		filters={"enrollment_type": enrollment_type},
	)
	student_map = _get_student_map([row.student for row in attendance_rows if row.student])
	parent_map = _get_parent_map([student.guardian for student in student_map.values() if student.get("guardian")])
	items = []
	for attendance in attendance_rows:
		session = session_map.get(attendance.course_session)
		timeslot = timeslot_map.get(session.weekly_timeslot) if session else None
		student = student_map.get(attendance.student)
		parent = parent_map.get(student.guardian) if student and student.get("guardian") else None
		items.append(
			{
				"type": "makeup_booking" if enrollment_type == "Makeup" else "adhoc_booking",
				"student": attendance.student,
				"student_name": student.get("student_name") if student else attendance.student,
				"parent": student.get("guardian") if student else None,
				"contact_name": parent.get("parent_name") if parent else None,
				"phone": parent.get("mobile_number") if parent else None,
				"email": None,
				"campus": timeslot.campus if timeslot else None,
				"course": timeslot.course if timeslot else None,
				"classroom": timeslot.classroom if timeslot else None,
				"date": str(session.session_date) if session else None,
				"time": str(timeslot.start_time) if timeslot else None,
				"status": attendance.status,
				"session_id": attendance.course_session,
				"attendance_entry": attendance.name,
				"latest_note": attendance.comments,
				"makeup_voucher": attendance.makeup_voucher,
			}
		)
	return items


def _get_adhoc_booking_dashboard_items(campuses, start_date, end_date):
	rows = frappe.get_all(
		"Adhoc Booking",
		filters={
			"campus": ["in", campuses],
			"class_date": ["between", [start_date, end_date]],
			"status": ["in", ["Reserved", "Locked"]],
		},
		fields=[
			"name",
			"parent",
			"student",
			"course",
			"course_session",
			"campus",
			"class_date",
			"start_time",
			"status",
			"payment_status",
		],
		order_by="class_date asc, start_time asc",
	)
	student_map = _get_student_map([row.student for row in rows if row.student])
	parent_map = _get_parent_map([row.parent for row in rows if row.parent])
	return [
		{
			"type": "adhoc_booking",
			"booking_id": row.name,
			"student": row.student,
			"student_name": student_map.get(row.student, {}).get("student_name") if row.student else None,
			"parent": row.parent,
			"contact_name": parent_map.get(row.parent, {}).get("parent_name") if row.parent else None,
			"phone": parent_map.get(row.parent, {}).get("mobile_number") if row.parent else None,
			"email": None,
			"campus": row.campus,
			"course": row.course,
			"classroom": None,
			"date": str(row.class_date) if row.class_date else None,
			"time": str(row.start_time) if row.start_time else None,
			"status": row.status,
			"payment_status": row.payment_status,
			"session_id": row.course_session,
			"latest_note": None,
			"makeup_voucher": None,
		}
		for row in rows
	]


def _build_inquiry_dashboard_item(row, student=None, latest_note=None):
	return {
		"type": "trial_lesson" if row.inquiry_type == "Trial Lesson" else "school_visit",
		"inquiry_id": row.name,
		"student": row.student,
		"student_name": student.get("student_name") if student else row.student,
		"parent": row.parent,
		"contact_name": row.contact_name,
		"phone": row.contact_phone,
		"email": row.contact_email,
		"campus": row.campus,
		"course": row.preferred_course,
		"date": str(row.current_appointment_date) if row.current_appointment_date else None,
		"time": str(row.current_appointment_time) if row.current_appointment_time else None,
		"status": row.status,
		"session_id": row.course_session,
		"latest_note": latest_note,
	}


def _build_inquiry_list_item(row):
	return {
		**build_inquiry_summary(row),
		"latest_note": _get_latest_note_map([row.name]).get(row.name),
	}


def _get_student_map(student_ids):
	student_ids = sorted({student_id for student_id in student_ids if student_id})
	if not student_ids:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Student",
			filters={"name": ["in", student_ids]},
			fields=["name", "student_name", "guardian"],
		)
	}


def _get_parent_map(parent_ids):
	parent_ids = sorted({parent_id for parent_id in parent_ids if parent_id})
	if not parent_ids:
		return {}
	return {
		row.name: row
		for row in frappe.get_all(
			"Parent",
			filters={"name": ["in", parent_ids]},
			fields=["name", "parent_name", "mobile_number"],
		)
	}


def _get_latest_note_map(inquiry_ids):
	if not inquiry_ids:
		return {}
	notes = frappe.get_all(
		"Inquiry Note",
		filters={"inquiry": ["in", inquiry_ids]},
		fields=["inquiry", "note", "creation"],
		order_by="creation desc",
	)
	latest = {}
	for note in notes:
		if note.inquiry not in latest:
			latest[note.inquiry] = note.note
	return latest
