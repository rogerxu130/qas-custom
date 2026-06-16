from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_time, getdate, now_datetime, today

from qas_custom.services.adhoc_attendance import (
	add_adhoc_attendance_row,
	remove_adhoc_attendance_row,
)
from qas_custom.services.adhoc_finance import (
	charge_booking_hold,
	get_customer_balance_summary,
	hold_booking_amount,
	release_booking_hold,
)

MINIMUM_NOTICE_HOURS = 72
PAY_AS_YOU_GO = "Pay-as-you-go"
ACTIVE_BOOKING_STATUSES = ("Reserved", "Locked")


def get_adhoc_home_data(student=None):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students) if student else None
	student_names = [selected_student] if selected_student else [row.name for row in students]
	customer = parent.get("customer")
	return {
		"students": [build_student_summary(row) for row in students],
		"upcoming_bookings": get_booking_items(parent.name, student_names, upcoming_only=True),
		"preferred_courses": get_preferred_course_items(student_names),
		"balance": get_customer_balance_summary(customer),
		"rules": get_adhoc_rules(),
	}


def get_adhoc_students_data():
	parent = require_parent()
	return {"students": [build_student_summary(row) for row in get_adhoc_students_for_parent(parent.name)]}


def get_preferred_courses_data(student=None):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students)
	return {"items": get_preferred_course_items([selected_student])}


def get_available_sessions_data(student=None, course=None, campus=None, date_from=None, date_to=None):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students)
	if not course:
		frappe.throw(_("Course is required."))

	start_date = getdate(date_from or today())
	end_date = getdate(date_to or add_days(start_date, 60))
	timeslot_filters = {"course": course}
	if campus:
		timeslot_filters["campus"] = campus

	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters=timeslot_filters,
		fields=["name", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		order_by="campus asc, start_time asc",
	)
	if not timeslots:
		return {"items": []}

	timeslot_map = {row.name: row for row in timeslots}
	sessions = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": ["in", list(timeslot_map.keys())],
			"session_date": ["between", [start_date, end_date]],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="session_date asc, name asc",
		limit=100,
	)

	items = []
	for session in sessions:
		timeslot = timeslot_map.get(session.weekly_timeslot)
		if not timeslot:
			continue
		session_start = get_session_start_datetime(session, timeslot)
		if not is_at_least_notice_period(session_start):
			continue
		if student_has_session_conflict(selected_student, session.name):
			continue
		items.append(build_session_item(session, timeslot, selected_student))
	return {"items": items}


def preview_booking_data(student=None, course_session=None):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students)
	context = validate_booking_context(parent, selected_student, course_session)
	fee_amount = get_trial_class_fee(context["timeslot"].get("course"))
	return {
		"student": build_student_summary(next(row for row in students if row.name == selected_student)),
		"session": build_session_item(context["session"], context["timeslot"], selected_student),
		"fee_amount": fee_amount,
		"balance": get_customer_balance_summary(parent.get("customer")),
		"rules": get_adhoc_rules(),
		"can_book": True,
	}


def create_booking_data(student=None, course_session=None, confirmed_rules=0):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students)
	context = validate_booking_context(parent, selected_student, course_session)
	session = context["session"]
	timeslot = context["timeslot"]
	session_start = context["session_start"]
	fee_amount = get_trial_class_fee(timeslot.get("course"))
	cancellable_until = session_start - timedelta(hours=MINIMUM_NOTICE_HOURS)

	booking = frappe.get_doc(
		{
			"doctype": "Adhoc Booking",
			"parent": parent.name,
			"student": selected_student,
			"customer": parent.get("customer"),
			"course": timeslot.get("course"),
			"course_session": session.name,
			"campus": timeslot.get("campus"),
			"class_date": session.get("session_date"),
			"start_time": timeslot.get("start_time"),
			"end_time": timeslot.get("end_time"),
			"fee_amount": fee_amount,
			"pricing_source": "Trial Class Fee",
			"status": "Reserved",
			"payment_status": "No Charge Yet",
			"balance_hold_amount": 0,
			"cancellable_until": cancellable_until,
			"created_by_portal_user": frappe.session.user,
		}
	)
	booking.insert(ignore_permissions=True)
	add_booking_history(booking.name, "booking_created", message="Adhoc booking created from portal.")

	try:
		hold = hold_booking_amount(booking)
		if hold:
			add_booking_history(booking.name, "balance_held", new_value=str(hold.amount))
		attendance_row_id = add_adhoc_attendance_row(session.name, selected_student, booking.name)
		booking.attendance_row_id = attendance_row_id
		add_booking_history(booking.name, "attendance_row_created", new_value=attendance_row_id)
		booking.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback()
		raise

	return {"booking": build_booking_item(booking), "rules": get_adhoc_rules()}


def get_bookings_data(student=None, status=None, include_history=0):
	parent = require_parent()
	students = get_adhoc_students_for_parent(parent.name)
	selected_student = validate_student_filter(student, students) if student else None
	student_names = [selected_student] if selected_student else [row.name for row in students]
	items = get_booking_items(parent.name, student_names, status=status)
	if int(include_history or 0):
		for item in items:
			item["history"] = get_booking_history_items(item["booking_id"])
	return {"items": items}


def cancel_booking_data(booking=None, reason=None):
	if not booking:
		frappe.throw(_("Booking is required."))
	parent = require_parent()
	doc = frappe.get_doc("Adhoc Booking", booking)
	if doc.parent != parent.name:
		frappe.throw(_("You do not have access to this booking."), frappe.PermissionError)
	if doc.status != "Reserved":
		add_booking_history(
			doc.name,
			"cancellation_requested_after_lock",
			message="Parent attempted to cancel a locked or closed booking.",
		)
		frappe.throw(_("This booking can no longer be cancelled online."))
	if now_datetime() >= doc.cancellable_until:
		add_booking_history(
			doc.name,
			"cancellation_requested_after_lock",
			message="Parent attempted to cancel within the three-day lock window.",
		)
		frappe.throw(_("Bookings cannot be refunded or cancelled online within three days of class."))

	removed = remove_adhoc_attendance_row(doc.course_session, doc.attendance_row_id)
	if removed:
		add_booking_history(doc.name, "attendance_row_removed", old_value=doc.attendance_row_id)
	else:
		add_booking_history(
			doc.name,
			"manual_adjustment",
			message="Attendance row was not found during cancellation.",
		)
	release_booking_hold(doc)
	add_booking_history(doc.name, "balance_released")
	doc.status = "Cancelled"
	doc.payment_status = "Released"
	doc.cancelled_at = now_datetime()
	doc.cancellation_reason = reason
	doc.save(ignore_permissions=True)
	add_booking_history(doc.name, "booking_cancelled_before_lock", message="Cancelled before lock window.")
	return {"booking": build_booking_item(doc), "cancelled": True}


def lock_due_bookings():
	rows = frappe.get_all(
		"Adhoc Booking",
		filters={"status": "Reserved", "cancellable_until": ["<=", now_datetime()]},
		fields=["name"],
		limit=200,
	)
	locked = []
	for row in rows:
		doc = frappe.get_doc("Adhoc Booking", row.name)
		charged_count = charge_booking_hold(doc)
		doc.status = "Locked"
		doc.locked_at = now_datetime()
		doc.save(ignore_permissions=True)
		add_booking_history(doc.name, "booking_locked", message="Booking entered three-day lock window.")
		if charged_count:
			add_booking_history(doc.name, "balance_charged", message="Held amount converted to charged.")
		else:
			add_booking_history(
				doc.name,
				"manual_adjustment",
				message="Booking locked without a confirmed balance hold; finance review is required.",
			)
		locked.append(doc.name)
	return {"locked": locked}


def require_parent():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
	if not parent_name:
		frappe.throw(_("No parent record is linked to this account."), frappe.PermissionError)
	return frappe.get_cached_doc("Parent", parent_name)


def get_adhoc_students_for_parent(parent_name: str):
	fields = ["name", "student_name", "age", "status", "guardian"]
	if frappe.db.has_column("Student", "enable_adhoc_portal"):
		fields.append("enable_adhoc_portal")
		filters = {"guardian": parent_name, "enable_adhoc_portal": 1}
	else:
		filters = {"guardian": parent_name}
	return frappe.get_all("Student", filters=filters, fields=fields, order_by="student_name asc")


def validate_student_filter(student: str | None, students: list[dict]):
	if not student:
		frappe.throw(_("Student is required."))
	allowed = {row.name for row in students}
	if student not in allowed:
		frappe.throw(_("This student is not available for Adhoc Portal booking."), frappe.PermissionError)
	return student


def validate_booking_context(parent, student: str, course_session: str | None):
	if not course_session:
		frappe.throw(_("Course session is required."))
	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "status"],
		as_dict=True,
	)
	if not session:
		frappe.throw(_("Course session was not found."))
	if session.get("status") == "Cancelled":
		frappe.throw(_("This course session is cancelled."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.get("weekly_timeslot"),
		["name", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))
	session_start = get_session_start_datetime(session, timeslot)
	if not is_at_least_notice_period(session_start):
		frappe.throw(_("Pay-as-you-go bookings must be made at least three days before class."))
	if student_has_session_conflict(student, session.name):
		frappe.throw(_("This student is already listed or booked for this course session."))
	return {"session": session, "timeslot": timeslot, "session_start": session_start}


def student_has_session_conflict(student: str, course_session: str):
	if frappe.db.exists(
		"Adhoc Booking",
		{"student": student, "course_session": course_session, "status": ["in", ACTIVE_BOOKING_STATUSES]},
	):
		return True
	if frappe.db.exists(
		"Attendance Record",
		{
			"parent": course_session,
			"parenttype": "Course Sessions",
			"parentfield": "attendance_list",
			"student": student,
		},
	):
		return True
	return False


def get_session_start_datetime(session, timeslot):
	if not session.get("session_date") or not timeslot.get("start_time"):
		frappe.throw(_("Course session is missing date or start time."))
	return datetime.combine(getdate(session.get("session_date")), get_time(timeslot.get("start_time")))


def is_at_least_notice_period(session_start: datetime):
	return session_start - now_datetime() >= timedelta(hours=MINIMUM_NOTICE_HOURS)


def get_trial_class_fee(course: str | None):
	if not course:
		return 0
	for fieldname in ("trial_class_fee", "trial_fee", "pay_as_you_go_fee", "session_fee", "fee"):
		if frappe.db.has_column("Course", fieldname):
			value = frappe.db.get_value("Course", course, fieldname)
			if value is not None:
				return flt(value)
	site_fee = frappe.conf.get("qas_adhoc_trial_class_fee")
	return flt(site_fee or 0)


def get_preferred_course_items(student_names: list[str]):
	student_names = [name for name in student_names if name]
	if not student_names:
		return []
	rows = frappe.get_all(
		"Adhoc Preferred Course",
		filters={"student": ["in", student_names], "enabled": 1},
		fields=["name", "student", "course", "campus", "priority", "note"],
		order_by="priority asc, modified desc",
	)
	return [dict(row) for row in rows]


def get_booking_items(parent: str, student_names: list[str], status=None, upcoming_only=False):
	if not student_names:
		return []
	filters = {"parent": parent, "student": ["in", student_names]}
	if status:
		filters["status"] = status
	if upcoming_only:
		filters["class_date"] = [">=", today()]
	rows = frappe.get_all(
		"Adhoc Booking",
		filters=filters,
		fields=[
			"name",
			"student",
			"course",
			"course_session",
			"campus",
			"class_date",
			"start_time",
			"end_time",
			"fee_amount",
			"status",
			"payment_status",
			"cancellable_until",
			"attendance_row_id",
		],
		order_by="class_date asc, start_time asc",
	)
	return [build_booking_item(row) for row in rows]


def build_student_summary(row):
	return {
		"name": row.name,
		"student_name": row.get("student_name") or row.name,
		"age": row.get("age"),
		"status": row.get("status"),
	}


def build_session_item(session, timeslot, student=None):
	session_start = get_session_start_datetime(session, timeslot)
	return {
		"session_id": session.name,
		"student": student,
		"course": timeslot.get("course"),
		"campus": timeslot.get("campus"),
		"classroom": timeslot.get("classroom"),
		"session_date": str(session.get("session_date")) if session.get("session_date") else None,
		"start_time": str(timeslot.get("start_time")) if timeslot.get("start_time") else None,
		"end_time": str(timeslot.get("end_time")) if timeslot.get("end_time") else None,
		"status": session.get("status"),
		"fee_amount": get_trial_class_fee(timeslot.get("course")),
		"cancellable_until": str(session_start - timedelta(hours=MINIMUM_NOTICE_HOURS)),
	}


def build_booking_item(row):
	now_dt = now_datetime()
	cancellable_until = row.get("cancellable_until")
	can_cancel = bool(row.get("status") == "Reserved" and cancellable_until and now_dt < cancellable_until)
	cancel_reason = None
	if row.get("status") == "Locked":
		cancel_reason = "Within three-day lock window"
	elif row.get("status") not in ("Reserved", None):
		cancel_reason = f"Booking is {row.get('status')}"
	elif not can_cancel:
		cancel_reason = "Booking can no longer be cancelled online"
	return {
		"booking_id": row.get("name"),
		"student": row.get("student"),
		"course": row.get("course"),
		"course_session": row.get("course_session"),
		"campus": row.get("campus"),
		"class_date": str(row.get("class_date")) if row.get("class_date") else None,
		"start_time": str(row.get("start_time")) if row.get("start_time") else None,
		"end_time": str(row.get("end_time")) if row.get("end_time") else None,
		"fee_amount": row.get("fee_amount"),
		"status": row.get("status"),
		"payment_status": row.get("payment_status"),
		"cancellable_until": str(cancellable_until) if cancellable_until else None,
		"can_cancel": can_cancel,
		"cancel_disabled_reason": cancel_reason,
		"attendance_row_id": row.get("attendance_row_id"),
	}


def add_booking_history(booking: str, event_type: str, old_value=None, new_value=None, message=None):
	frappe.get_doc(
		{
			"doctype": "Adhoc Booking History",
			"adhoc_booking": booking,
			"event_type": event_type,
			"event_time": now_datetime(),
			"actor": frappe.session.user if frappe.session.user != "Guest" else None,
			"old_value": old_value,
			"new_value": new_value,
			"message": message,
		}
	).insert(ignore_permissions=True)


def get_booking_history_items(booking: str):
	return frappe.get_all(
		"Adhoc Booking History",
		filters={"adhoc_booking": booking},
		fields=["event_type", "event_time", "actor", "old_value", "new_value", "message"],
		order_by="event_time desc",
	)


def get_adhoc_rules():
	return {
		"minimum_booking_notice_days": 3,
		"minimum_booking_notice_hours": MINIMUM_NOTICE_HOURS,
		"free_cancel_before_hours": MINIMUM_NOTICE_HOURS,
		"voucher_policy": "No makeup voucher for Pay-as-you-go bookings",
		"locked_cancel_policy": "Bookings cannot be refunded online within three days of class.",
	}
