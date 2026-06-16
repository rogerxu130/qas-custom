import frappe

from qas_custom.services.adhoc_booking import (
	cancel_booking_data,
	create_booking_data,
	get_adhoc_home_data,
	get_adhoc_students_data,
	get_available_sessions_data,
	get_bookings_data,
	get_preferred_courses_data,
	preview_booking_data,
)


@frappe.whitelist()
def adhoc_portal_get_home(student=None):
	return get_adhoc_home_data(student=student)


@frappe.whitelist()
def adhoc_portal_get_students():
	return get_adhoc_students_data()


@frappe.whitelist()
def adhoc_portal_get_preferred_courses(student=None):
	return get_preferred_courses_data(student=student)


@frappe.whitelist()
def adhoc_portal_get_available_sessions(student=None, course=None, campus=None, date_from=None, date_to=None):
	return get_available_sessions_data(
		student=student,
		course=course,
		campus=campus,
		date_from=date_from,
		date_to=date_to,
	)


@frappe.whitelist()
def adhoc_portal_preview_booking(student=None, course_session=None):
	return preview_booking_data(student=student, course_session=course_session)


@frappe.whitelist()
def adhoc_portal_create_booking(student=None, course_session=None, confirmed_rules=0):
	return create_booking_data(
		student=student,
		course_session=course_session,
		confirmed_rules=confirmed_rules,
	)


@frappe.whitelist()
def adhoc_portal_get_bookings(student=None, status=None, include_history=0):
	return get_bookings_data(student=student, status=status, include_history=include_history)


@frappe.whitelist()
def adhoc_portal_cancel_booking(booking=None, reason=None):
	return cancel_booking_data(booking=booking, reason=reason)
