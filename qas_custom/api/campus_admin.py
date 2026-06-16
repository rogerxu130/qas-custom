import frappe

from qas_custom.services.campus_admin import (
	add_campus_admin_inquiry_note_data,
	assign_campus_admin_inquiry_course_session_data,
	get_campus_admin_dashboard_data,
	get_campus_admin_inquiries_data,
	get_campus_admin_inquiry_data,
	get_campus_admin_me_data,
	mark_campus_admin_inquiry_completed_data,
	mark_campus_admin_inquiry_follow_up_data,
	mark_campus_admin_inquiry_no_show_data,
	reschedule_campus_admin_inquiry_data,
)


@frappe.whitelist()
def campus_admin_get_me():
	return get_campus_admin_me_data()


@frappe.whitelist()
def campus_admin_get_dashboard(from_date=None, to_date=None):
	return get_campus_admin_dashboard_data(from_date=from_date, to_date=to_date)


@frappe.whitelist()
def campus_admin_get_inquiries(status=None, inquiry_type=None, from_date=None, to_date=None, campus=None):
	return get_campus_admin_inquiries_data(
		status=status,
		inquiry_type=inquiry_type,
		from_date=from_date,
		to_date=to_date,
		campus=campus,
	)


@frappe.whitelist()
def campus_admin_get_inquiry(inquiry=None):
	return get_campus_admin_inquiry_data(inquiry=inquiry)


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
def campus_admin_mark_inquiry_follow_up(inquiry=None):
	return mark_campus_admin_inquiry_follow_up_data(inquiry=inquiry)


@frappe.whitelist()
def campus_admin_reschedule_inquiry(inquiry=None, payload=None):
	return reschedule_campus_admin_inquiry_data(inquiry=inquiry, payload=payload)


@frappe.whitelist()
def campus_admin_assign_inquiry_course_session(inquiry=None, course_session=None):
	return assign_campus_admin_inquiry_course_session_data(inquiry=inquiry, course_session=course_session)
