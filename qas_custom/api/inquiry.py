import frappe

from qas_custom.services.inquiry import (
	add_inquiry_note_data,
	assign_inquiry_course_session_data,
	create_inquiry_data,
	create_inquiry_webhook_data,
	get_inquiry_data,
	mark_inquiry_completed_data,
	mark_inquiry_follow_up_data,
	mark_inquiry_no_show_data,
	reschedule_inquiry_data,
)


@frappe.whitelist()
def inquiry_create(payload=None):
	return create_inquiry_data(payload=payload, source="Manual")


@frappe.whitelist(allow_guest=True)
def inquiry_webhook_create(payload=None):
	return create_inquiry_webhook_data(payload=payload)


@frappe.whitelist()
def inquiry_get(inquiry=None):
	return get_inquiry_data(inquiry=inquiry)


@frappe.whitelist()
def inquiry_reschedule(inquiry=None, payload=None):
	return reschedule_inquiry_data(inquiry=inquiry, payload=payload)


@frappe.whitelist()
def inquiry_assign_course_session(inquiry=None, course_session=None):
	return assign_inquiry_course_session_data(inquiry=inquiry, course_session=course_session)


@frappe.whitelist()
def inquiry_mark_completed(inquiry=None):
	return mark_inquiry_completed_data(inquiry=inquiry)


@frappe.whitelist()
def inquiry_mark_no_show(inquiry=None):
	return mark_inquiry_no_show_data(inquiry=inquiry)


@frappe.whitelist()
def inquiry_mark_follow_up(inquiry=None):
	return mark_inquiry_follow_up_data(inquiry=inquiry)


@frappe.whitelist()
def inquiry_add_note(inquiry=None, note=None):
	return add_inquiry_note_data(inquiry=inquiry, note=note)
