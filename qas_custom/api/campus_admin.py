import frappe

from qas_custom.services.campus_admin import (
	add_campus_admin_inquiry_note_data,
	convert_campus_admin_inquiry_data,
	get_campus_admin_csrf_token_data,
	get_campus_admin_conversion_sessions_data,
	get_campus_admin_dashboard_data,
	get_campus_admin_inquiries_data,
	get_campus_admin_inquiry_data,
	get_campus_admin_me_data,
	mark_campus_admin_inquiry_cancelled_data,
	mark_campus_admin_inquiry_completed_data,
	mark_campus_admin_inquiry_follow_up_data,
	mark_campus_admin_inquiry_inactive_data,
	mark_campus_admin_inquiry_no_show_data,
)


@frappe.whitelist()
def campus_admin_get_me():
	return get_campus_admin_me_data()


@frappe.whitelist()
def campus_admin_get_csrf_token():
	return get_campus_admin_csrf_token_data()


@frappe.whitelist()
def campus_admin_get_dashboard(from_date=None, to_date=None):
	return get_campus_admin_dashboard_data(from_date=from_date, to_date=to_date)


@frappe.whitelist()
def campus_admin_get_inquiries(status=None, inquiry_type=None, from_date=None, to_date=None, campus=None, queue=None):
	return get_campus_admin_inquiries_data(
		status=status,
		inquiry_type=inquiry_type,
		from_date=from_date,
		to_date=to_date,
		campus=campus,
		queue=queue,
	)


@frappe.whitelist()
def campus_admin_get_inquiry(inquiry=None):
	return get_campus_admin_inquiry_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_get_course_sessions(campus=None, course=None, from_date=None, to_date=None, query=None):
	frappe.throw("Trial lesson scheduling is managed by School Admin.", frappe.PermissionError)


@frappe.whitelist()
def campus_admin_add_inquiry_note(inquiry=None, note=None):
	return add_campus_admin_inquiry_note_data(inquiry=inquiry, note=note)


@frappe.whitelist()
def campus_admin_mark_inquiry_completed(inquiry=None):
	return mark_campus_admin_inquiry_completed_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_mark_inquiry_no_show(inquiry=None):
	return mark_campus_admin_inquiry_no_show_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_mark_inquiry_cancelled(inquiry=None):
	return mark_campus_admin_inquiry_cancelled_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_mark_inquiry_follow_up(inquiry=None):
	return mark_campus_admin_inquiry_follow_up_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_get_conversion_sessions(inquiry=None, start_date=None, course=None):
	return get_campus_admin_conversion_sessions_data(inquiry=inquiry, start_date=start_date, course=course)


@frappe.whitelist()
def campus_admin_convert_inquiry(inquiry=None, course_session=None):
	return convert_campus_admin_inquiry_data(inquiry=inquiry, course_session=course_session)


@frappe.whitelist()
def campus_admin_mark_inquiry_inactive(inquiry=None, inactive_reason=None):
	return mark_campus_admin_inquiry_inactive_data(inquiry=inquiry, inactive_reason=inactive_reason)


@frappe.whitelist()
def campus_admin_reschedule_inquiry(inquiry=None, payload=None):
	frappe.throw("Trial lesson scheduling is managed by School Admin.", frappe.PermissionError)


@frappe.whitelist()
def campus_admin_assign_inquiry_course_session(inquiry=None, course_session=None):
	frappe.throw("Trial lesson scheduling is managed by School Admin.", frappe.PermissionError)
