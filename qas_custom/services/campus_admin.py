from __future__ import annotations

import frappe
from frappe import _

from frappe.utils import add_days, cint, getdate, now_datetime, today

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.services.class_attendance import get_attendance_entries
from qas_custom.services.display_labels import get_makeup_voucher_label, get_student_display_name
from qas_custom.services.inquiry import (
	add_inquiry_note_core,
	build_inquiry_detail,
	build_inquiry_summary,
	mark_inquiry_status_core,
	send_trial_class_reminder_core,
)
from qas_custom.services.school_admin import (
	_count_leave_attendance_rows,
	_course_session_sort_key,
	_document_payload,
	_get_course_session_rows,
	_get_school_admin_attendance_rows,
	_get_timeslot_summary,
	_roster_course_session_attendance_rows,
	_visible_course_session_attendance_rows,
)
from qas_custom.services.teacher_directory import get_active_teacher_directory_data
from qas_custom.services.support_view import get_support_view_campus_admin_profile, reject_support_view_write


POST_VISIT_INQUIRY_STATUSES = ("Completed", "Follow-up", "No-show", "Converted", "Inactive")
CAMPUS_ADMIN_INQUIRY_RESULT_LIMIT = 200
CAMPUS_ADMIN_INQUIRY_SEARCH_FIELDS = (
	"name",
	"submitted_student_name",
	"student",
	"parent",
	"contact_name",
	"contact_email",
	"contact_phone",
)


def get_campus_admin_me_data():
	profile = _require_campus_admin_profile()
	return {
		"user": profile["user"],
		"profile": profile["name"],
		"active": True,
		"campuses": profile["campuses"],
	}


def get_campus_admin_csrf_token_data():
	_require_campus_admin_profile()
	return {
		"csrf_token": frappe.sessions.get_csrf_token(),
	}


def get_campus_admin_teacher_directory_data(query=None, limit=300):
	_require_campus_admin_profile()
	return get_active_teacher_directory_data(query=query, limit=limit)


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


def get_campus_admin_inquiries_data(
	status=None,
	inquiry_type=None,
	from_date=None,
	to_date=None,
	campus=None,
	queue=None,
	query=None,
	course=None,
	limit=None,
):
	profile = _require_campus_admin_profile()
	campuses = _filter_requested_campus(profile["campuses"], campus)
	filters = {
		"campus": ["in", campuses],
	}
	if status:
		filters["status"] = status
	if inquiry_type:
		filters["inquiry_type"] = inquiry_type
	if course:
		filters["preferred_course"] = course
	queue_filters, or_filters = _campus_admin_inquiry_queue_filters(queue, status=status)
	filters.update(queue_filters)
	queue_date_filter = filters.pop("current_appointment_date", None)
	date_filter = _campus_admin_inquiry_date_filter(queue_date_filter, from_date=from_date, to_date=to_date)
	if date_filter:
		filters["current_appointment_date"] = date_filter
	elif date_filter is False:
		filters["name"] = "__qas_no_matching_inquiry__"

	page_limit = min(max(cint(limit or CAMPUS_ADMIN_INQUIRY_RESULT_LIMIT), 1), CAMPUS_ADMIN_INQUIRY_RESULT_LIMIT)
	order_by = (
		"current_appointment_date desc, current_appointment_time desc, modified desc"
		if queue == "post_trial"
		else "current_appointment_date asc, current_appointment_time asc, modified desc"
	)
	matching_names = _campus_admin_inquiry_search_names(
		filters,
		or_filters,
		query,
		order_by=order_by,
		limit=page_limit + 1,
	)
	if matching_names is not None:
		if not matching_names:
			return {"items": [], "has_more": False, "limit": page_limit}
		filters["name"] = ["in", matching_names]

	rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		or_filters=or_filters,
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
			"submitted_student_name",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
		],
		order_by=order_by,
		limit_page_length=page_limit + 1,
	)
	has_more = len(rows) > page_limit
	rows = rows[:page_limit]
	latest_notes = _get_latest_note_map([row.name for row in rows])
	return {
		"items": [_build_inquiry_list_item(row, latest_note=latest_notes.get(row.name)) for row in rows],
		"has_more": has_more,
		"limit": page_limit,
	}


def get_campus_admin_inquiry_filter_options_data(campus=None):
	profile = _require_campus_admin_profile()
	assigned_campuses = list(profile["campuses"])
	selected_campuses = _filter_requested_campus(assigned_campuses, campus)
	rows = frappe.get_all(
		"Inquiry",
		filters={
			"campus": ["in", selected_campuses],
			"preferred_course": ["is", "set"],
		},
		fields=["preferred_course"],
		group_by="preferred_course",
		order_by="preferred_course asc",
		limit_page_length=1000,
	)
	return {
		"campuses": assigned_campuses,
		"courses": [row.preferred_course for row in rows if row.preferred_course],
	}


def _campus_admin_inquiry_date_filter(queue_filter=None, *, from_date=None, to_date=None):
	start_date = getdate(from_date) if from_date else None
	end_date = getdate(to_date) if to_date else None
	if start_date and end_date and start_date > end_date:
		frappe.throw(_("From date cannot be later than To date."))

	if queue_filter:
		operator, value = queue_filter
		value = getdate(value)
		if operator == ">=":
			start_date = max(filter(None, [start_date, value]))
		elif operator == "<":
			queue_end = add_days(value, -1)
			end_date = min(filter(None, [end_date, queue_end]))
		else:
			return queue_filter

	if start_date and end_date and start_date > end_date:
		return False
	if start_date and end_date:
		return ["between", [start_date, end_date]]
	if start_date:
		return [">=", start_date]
	if end_date:
		return ["<=", end_date]
	return None


def _campus_admin_inquiry_search_names(filters, queue_or_filters, query, *, order_by, limit):
	query = str(query or "").strip()
	if not query:
		return None
	if filters.get("name") == "__qas_no_matching_inquiry__":
		return []

	pattern = f"%{query}%"
	names = set()
	if not queue_or_filters:
		names.update(
			frappe.get_all(
				"Inquiry",
				filters=filters,
				or_filters=[
					["Inquiry", fieldname, "like", pattern]
					for fieldname in CAMPUS_ADMIN_INQUIRY_SEARCH_FIELDS
				],
				pluck="name",
				order_by=order_by,
				limit_page_length=limit,
			)
		)
	else:
		for fieldname in CAMPUS_ADMIN_INQUIRY_SEARCH_FIELDS:
			field_filters = dict(filters)
			field_filters[fieldname] = ["like", pattern]
			names.update(
				frappe.get_all(
					"Inquiry",
					filters=field_filters,
					or_filters=queue_or_filters,
					pluck="name",
					order_by=order_by,
					limit_page_length=limit,
				)
			)

	student_ids = _campus_admin_link_matches(
		"Student",
		_safe_fields("Student", ["name", "student_name", "student_code"]),
		pattern,
		limit=limit,
	)
	if student_ids:
		field_filters = dict(filters)
		field_filters["student"] = ["in", student_ids]
		names.update(
			frappe.get_all(
				"Inquiry",
				filters=field_filters,
				or_filters=queue_or_filters,
				pluck="name",
				order_by=order_by,
				limit_page_length=limit,
			)
		)

	parent_ids = _campus_admin_link_matches(
		"Parent",
		_safe_fields("Parent", ["name", "parent_name"]),
		pattern,
		limit=limit,
	)
	if parent_ids:
		field_filters = dict(filters)
		field_filters["parent"] = ["in", parent_ids]
		names.update(
			frappe.get_all(
				"Inquiry",
				filters=field_filters,
				or_filters=queue_or_filters,
				pluck="name",
				order_by=order_by,
				limit_page_length=limit,
			)
		)
	return list(names)


def _campus_admin_link_matches(doctype, fieldnames, pattern, *, limit):
	if not fieldnames:
		return []
	return frappe.get_all(
		doctype,
		or_filters=[[doctype, fieldname, "like", pattern] for fieldname in fieldnames],
		pluck="name",
		limit_page_length=limit,
	)


def _campus_admin_inquiry_queue_filters(queue, status=None, reference_date=None):
	reference_date = getdate(reference_date or today())
	if queue == "upcoming":
		if status in POST_VISIT_INQUIRY_STATUSES:
			return {"name": "__qas_no_matching_inquiry__"}, None
		filters = {"current_appointment_date": [">=", reference_date]}
		if not status:
			filters["status"] = ["not in", list(POST_VISIT_INQUIRY_STATUSES)]
		return filters, None
	if queue == "post_trial":
		if status:
			if status in POST_VISIT_INQUIRY_STATUSES:
				return {}, None
			return {"current_appointment_date": ["<", reference_date]}, None
		return {}, [
			["Inquiry", "status", "in", list(POST_VISIT_INQUIRY_STATUSES)],
			["Inquiry", "current_appointment_date", "<", reference_date],
		]
	return {}, None


def get_campus_admin_inquiry_data(inquiry=None):
	_require_inquiry_access(inquiry)
	return build_inquiry_detail(inquiry)


def send_campus_admin_trial_class_reminder_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return send_trial_class_reminder_core(inquiry=inquiry)


def get_campus_admin_contacts_data(from_date=None, to_date=None, campus=None, course_session=None, query=None):
	profile = _require_campus_admin_profile()
	campuses = _filter_requested_campus(profile["campuses"], campus)
	start_date = getdate(from_date or today())
	end_date = getdate(to_date or add_days(start_date, 14))

	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={"campus": ["in", campuses]},
		fields=["name", "course", "class_language", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time"],
	)
	if not timeslots:
		return {"sessions": [], "contacts": []}

	timeslot_map = {row.name: row for row in timeslots}
	session_filters = {"weekly_timeslot": ["in", list(timeslot_map.keys())]}
	if course_session:
		session_filters["name"] = course_session
	else:
		session_filters["session_date"] = ["between", [start_date, end_date]]

	sessions = frappe.get_all(
		"Course Sessions",
		filters=session_filters,
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, modified asc",
	)
	if not sessions:
		return {"sessions": [], "contacts": []}

	session_map = {row.name: row for row in sessions}
	attendance_rows = get_attendance_entries(
		list(session_map.keys()),
		fields=[
			"name",
			"course_session",
			"student",
			"enrollment_type",
			"status",
			"comments",
			"makeup_voucher",
			"source_doctype",
			"source_document",
		],
	)
	student_map = _get_student_map([row.student for row in attendance_rows if row.student])
	parent_map = _get_parent_map([student.guardian for student in student_map.values() if student.get("guardian")])

	contacts = []
	for attendance in attendance_rows:
		session = session_map.get(attendance.course_session)
		timeslot = timeslot_map.get(session.weekly_timeslot) if session else None
		student = student_map.get(attendance.student)
		parent = parent_map.get(student.guardian) if student and student.get("guardian") else None
		item = {
			"attendance_entry": attendance.name,
			"course_session": attendance.course_session,
			"session_date": str(session.session_date) if session else None,
			"session_status": session.status if session else None,
			"course": timeslot.course if timeslot else None,
			"class_language": (timeslot.get("class_language") if timeslot else None) or "English",
			"campus": timeslot.campus if timeslot else None,
			"classroom": timeslot.classroom if timeslot else None,
			"teacher": timeslot.teacher if timeslot else None,
			"day_of_week": timeslot.day_of_week if timeslot else None,
			"start_time": str(timeslot.start_time) if timeslot else None,
			"end_time": str(timeslot.end_time) if timeslot else None,
			"student": attendance.student,
			"student_name": get_student_display_name(student) if student else attendance.student,
			"parent": student.get("guardian") if student else None,
			"parent_name": parent.get("parent_name") if parent else None,
			"phone": parent.get("mobile_number") if parent else None,
			"email": (parent.get("email") or parent.get("email_id")) if parent else None,
			"enrollment_type": attendance.enrollment_type,
			"attendance_status": attendance.status,
			"source_doctype": attendance.source_doctype,
			"source_document": attendance.source_document,
			"makeup_voucher": attendance.makeup_voucher,
			"makeup_voucher_label": get_makeup_voucher_label(attendance.makeup_voucher),
			"comments": attendance.comments,
		}
		if _contact_matches_query(item, query):
			contacts.append(item)

	session_counts = {}
	for item in contacts:
		session_counts[item["course_session"]] = session_counts.get(item["course_session"], 0) + 1
	visible_sessions = []
	for session in sessions:
		timeslot = timeslot_map.get(session.weekly_timeslot)
		if query and not session_counts.get(session.name) and not _contact_session_matches_query(session, timeslot, query):
			continue
		visible_sessions.append(session)

	return {
		"sessions": [
			_build_contact_session_item(session, timeslot_map.get(session.weekly_timeslot), session_counts.get(session.name, 0))
			for session in visible_sessions
		],
		"contacts": contacts,
	}


def get_campus_admin_course_sessions_data(
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=160,
):
	profile = _require_campus_admin_profile()
	campuses = _filter_requested_campus(profile["campuses"], campus)
	row_limit = min(max(cint(limit or 160), 1), 3000)
	items_by_name = {}
	for allowed_campus in campuses:
		for item in _get_course_session_rows(
			term=term,
			course=course,
			campus=allowed_campus,
			from_date=from_date,
			to_date=to_date,
			include_inactive_terms=include_inactive_terms,
			include_inactive_timeslots=include_inactive_timeslots,
			limit=row_limit,
		):
			if item.get("name"):
				items_by_name[item["name"]] = item

	items = sorted(
		items_by_name.values(),
		key=_course_session_sort_key,
	)
	_attach_campus_admin_teacher_labels(items)
	return {"items": items[:row_limit]}


def get_campus_admin_course_session_data(course_session=None):
	profile = _require_campus_admin_profile()
	if not course_session:
		frappe.throw(_("Course session is required."))

	doc, timeslot = _get_campus_admin_course_session_access(course_session, profile["campuses"])
	payload = _document_payload(doc)
	payload["weekly_timeslot_detail"] = _get_timeslot_summary(timeslot.name)
	attendance_rows = _get_school_admin_attendance_rows(
		course_session,
		term=(payload.get("weekly_timeslot_detail") or {}).get("term"),
	)
	attending_rows = _visible_course_session_attendance_rows(attendance_rows)
	payload["attendance"] = _roster_course_session_attendance_rows(attendance_rows)
	payload["student_count"] = len(attending_rows)
	payload["trial_count"] = sum(1 for row in attending_rows if row.get("source_doctype") == "Inquiry")
	payload["leave_count"] = _count_leave_attendance_rows(attendance_rows)
	timeslot_teacher = (payload.get("weekly_timeslot_detail") or {}).get("teacher")
	payload["teacher"] = payload.get("teacher_override") or timeslot_teacher
	_attach_campus_admin_teacher_labels([payload])
	payload["teacher_assignment_source"] = "Session override" if payload.get("teacher_override") else "Weekly timeslot"
	return payload


def update_campus_admin_student_teaching_notes_data(student=None, teaching_notes=None):
	reject_support_view_write()
	profile = _require_campus_admin_profile()
	if not student:
		frappe.throw(_("Student is required."))
	if not frappe.db.exists("Student", student):
		frappe.throw(_("Student was not found."), frappe.DoesNotExistError)
	if "teaching_notes" not in _safe_fields("Student", ["teaching_notes"]):
		frappe.throw(_("Student teaching notes are not available on this site. Please run migrate."))
	_assert_campus_admin_student_access(student, profile["campuses"])

	doc = frappe.get_doc("Student", student)
	doc.teaching_notes = str(teaching_notes or "").strip()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"student": doc.name, "teaching_notes": doc.get("teaching_notes") or ""}


def _assert_campus_admin_student_access(student, allowed_campuses):
	attendance_sessions = frappe.get_all(
		"Class Attendance Entry",
		filters={"student": student},
		pluck="course_session",
		limit_page_length=0,
	)
	attendance_sessions = sorted({session for session in attendance_sessions if session})
	if attendance_sessions:
		weekly_timeslots = frappe.get_all(
			"Course Sessions",
			filters={"name": ["in", attendance_sessions]},
			pluck="weekly_timeslot",
			limit_page_length=0,
		)
		weekly_timeslots = sorted({timeslot for timeslot in weekly_timeslots if timeslot})
		if weekly_timeslots and frappe.get_all(
			"Weekly Timeslot",
			filters={"name": ["in", weekly_timeslots], "campus": ["in", allowed_campuses]},
			pluck="name",
			limit=1,
		):
			return
	frappe.throw(_("You do not have access to this Student."), frappe.PermissionError)


def _get_campus_admin_course_session_access(course_session, allowed_campuses):
	try:
		doc = frappe.get_doc("Course Sessions", course_session)
	except frappe.DoesNotExistError:
		frappe.throw(_("Course session was not found."), frappe.DoesNotExistError)
	weekly_timeslot = doc.get("weekly_timeslot")
	if not weekly_timeslot:
		frappe.throw(_("Course session has no weekly timeslot."), frappe.PermissionError)
	timeslot = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	if timeslot.get("campus") not in allowed_campuses:
		frappe.throw(_("You do not have access to this course session."), frappe.PermissionError)
	return doc, timeslot


def _attach_campus_admin_teacher_labels(items):
	teacher_ids = sorted({item.get("teacher") for item in items if item.get("teacher")})
	if not teacher_ids:
		return items
	teacher_map = {
		row.get("name"): row.get("teacher_name") or row.get("name")
		for row in frappe.get_all(
			"Teacher",
			filters={"name": ["in", teacher_ids]},
			fields=["name", "teacher_name"],
			limit_page_length=0,
		)
	}
	for item in items:
		item["teacher_display"] = teacher_map.get(item.get("teacher"), item.get("teacher") or "")
	return items


def add_campus_admin_inquiry_note_data(inquiry=None, note=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)


def mark_campus_admin_inquiry_completed_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Completed", actor=frappe.session.user)


def mark_campus_admin_inquiry_no_show_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "No-show", actor=frappe.session.user)


def mark_campus_admin_inquiry_cancelled_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Cancelled", actor=frappe.session.user)


def reopen_campus_admin_inquiry_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	if not inquiry:
		frappe.throw(_("Inquiry is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status not in {"Cancelled", "Completed"}:
		frappe.throw(_("Only completed or cancelled inquiries can be reopened."))

	previous_status = inquiry_doc.status
	target_status = "Booked" if previous_status == "Completed" else _get_reopen_status(inquiry_doc)
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
	reject_support_view_write()
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


def convert_campus_admin_inquiry_data(inquiry=None, course_session=None, internal_note=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	_validate_conversion_session_access(inquiry, course_session)
	result = convert_inquiry_to_full_term_core(
		inquiry,
		course_session,
		actor=frappe.session.user,
		internal_note=internal_note,
	)
	return result["inquiry"]


def mark_campus_admin_inquiry_inactive_data(inquiry=None, inactive_reason=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return mark_inquiry_inactive_core(inquiry, inactive_reason, actor=frappe.session.user)


def _require_campus_admin_profile():
	support_profile = get_support_view_campus_admin_profile()
	if support_profile:
		campuses = [row.campus for row in support_profile.get("campuses", []) if row.campus]
		if not campuses:
			frappe.throw(_("Campus Admin profile has no assigned campuses."), frappe.PermissionError)
		return {"name": support_profile.name, "user": support_profile.user, "campuses": campuses}
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
	return {"name": doc.name, "user": doc.user, "campuses": campuses}


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
		fields=["name", "course", "class_language", "campus", "classroom", "start_time", "end_time"],
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
				"student_name": get_student_display_name(student) if student else attendance.student,
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
				"makeup_voucher_label": get_makeup_voucher_label(attendance.makeup_voucher),
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


def _build_inquiry_list_item(row, latest_note=None):
	return {
		**build_inquiry_summary(row),
		"latest_note": latest_note,
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
			fields=_safe_fields("Student", ["name", "student_name", "student_code", "guardian"]),
		)
	}


def _get_parent_map(parent_ids):
	parent_ids = sorted({parent_id for parent_id in parent_ids if parent_id})
	if not parent_ids:
		return {}
	fields = _safe_fields("Parent", ["name", "parent_name", "mobile_number", "email", "email_id"])
	return {
		row.name: row
		for row in frappe.get_all(
			"Parent",
			filters={"name": ["in", parent_ids]},
			fields=fields,
		)
	}


def _build_contact_session_item(session, timeslot, contact_count=0):
	return {
		"id": session.name,
		"course_session": session.name,
		"session_date": str(session.session_date) if session.session_date else None,
		"status": session.status,
		"course": timeslot.course if timeslot else None,
		"class_language": (timeslot.get("class_language") if timeslot else None) or "English",
		"campus": timeslot.campus if timeslot else None,
		"classroom": timeslot.classroom if timeslot else None,
		"teacher": timeslot.teacher if timeslot else None,
		"start_time": str(timeslot.start_time) if timeslot else None,
		"end_time": str(timeslot.end_time) if timeslot else None,
		"student_count": contact_count,
	}


def _contact_matches_query(item, query=None):
	if not query:
		return True
	needle = str(query).strip().lower()
	if not needle:
		return True
	values = [
		item.get("student"),
		item.get("student_name"),
		item.get("parent"),
		item.get("parent_name"),
		item.get("phone"),
		item.get("email"),
		item.get("course"),
		item.get("campus"),
		item.get("classroom"),
		item.get("teacher"),
		item.get("day_of_week"),
		item.get("session_status"),
		item.get("attendance_status"),
		item.get("course_session"),
		item.get("enrollment_type"),
		item.get("source_doctype"),
		item.get("source_document"),
	]
	return any(needle in str(value).lower() for value in values if value)


def _contact_session_matches_query(session, timeslot, query=None):
	if not query:
		return True
	needle = str(query).strip().lower()
	if not needle:
		return True
	values = [
		session.name,
		session.status,
		session.session_date,
		timeslot.course if timeslot else None,
		timeslot.campus if timeslot else None,
		timeslot.classroom if timeslot else None,
		timeslot.teacher if timeslot else None,
		timeslot.day_of_week if timeslot else None,
		timeslot.start_time if timeslot else None,
		timeslot.end_time if timeslot else None,
	]
	return any(needle in str(value).lower() for value in values if value)


def _safe_fields(doctype, candidates):
	meta = frappe.get_meta(doctype)
	return [fieldname for fieldname in candidates if fieldname == "name" or meta.has_field(fieldname)]


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
