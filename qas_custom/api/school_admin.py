import frappe

from qas_custom.services.school_admin import (
	add_school_admin_inquiry_note_data,
	cancel_school_admin_invoice_data,
	convert_school_admin_inquiry_data,
	create_school_admin_manual_invoice_data,
	get_school_admin_conversion_sessions_data,
	get_school_admin_course_session_data,
	get_school_admin_course_sessions_data,
	get_school_admin_csrf_token_data,
	get_school_admin_dashboard_data,
	get_school_admin_enrollment_data,
	get_school_admin_enrollments_data,
	get_school_admin_family_data,
	get_school_admin_inquiries_data,
	get_school_admin_inquiry_data,
	get_school_admin_invoice_data,
	get_school_admin_invoices_data,
	get_school_admin_me_data,
	get_school_admin_weekly_timeslot_data,
	get_school_admin_weekly_timeslots_data,
	mark_school_admin_inquiry_completed_data,
	mark_school_admin_inquiry_follow_up_data,
	mark_school_admin_inquiry_inactive_data,
	mark_school_admin_inquiry_no_show_data,
	reschedule_school_admin_inquiry_data,
	school_admin_global_search_data,
	submit_school_admin_invoice_data,
	update_school_admin_draft_invoice_data,
	update_school_admin_inquiry_status_data,
)


@frappe.whitelist()
def school_admin_get_me():
	return get_school_admin_me_data()


@frappe.whitelist()
def school_admin_get_csrf_token():
	return get_school_admin_csrf_token_data()


@frappe.whitelist()
def school_admin_get_dashboard():
	return get_school_admin_dashboard_data()


@frappe.whitelist()
def school_admin_global_search(query=None, limit=20):
	return school_admin_global_search_data(query=query, limit=limit)


@frappe.whitelist()
def school_admin_get_family(parent=None, student=None, customer=None, email=None):
	return get_school_admin_family_data(parent=parent, student=student, customer=customer, email=email)


@frappe.whitelist()
def school_admin_get_inquiries(status=None, inquiry_type=None, campus=None, from_date=None, to_date=None, queue=None, limit=80):
	return get_school_admin_inquiries_data(
		status=status,
		inquiry_type=inquiry_type,
		campus=campus,
		from_date=from_date,
		to_date=to_date,
		queue=queue,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_inquiry(inquiry=None):
	return get_school_admin_inquiry_data(inquiry=inquiry)


@frappe.whitelist()
def school_admin_add_inquiry_note(inquiry=None, note=None):
	return add_school_admin_inquiry_note_data(inquiry=inquiry, note=note)


@frappe.whitelist()
def school_admin_update_inquiry_status(inquiry=None, status=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status=status)


@frappe.whitelist()
def school_admin_mark_inquiry_completed(inquiry=None):
	return mark_school_admin_inquiry_completed_data(inquiry=inquiry)


@frappe.whitelist()
def school_admin_mark_inquiry_no_show(inquiry=None):
	return mark_school_admin_inquiry_no_show_data(inquiry=inquiry)


@frappe.whitelist()
def school_admin_mark_inquiry_follow_up(inquiry=None):
	return mark_school_admin_inquiry_follow_up_data(inquiry=inquiry)


@frappe.whitelist()
def school_admin_mark_inquiry_inactive(inquiry=None, inactive_reason=None):
	return mark_school_admin_inquiry_inactive_data(inquiry=inquiry, inactive_reason=inactive_reason)


@frappe.whitelist()
def school_admin_reschedule_inquiry(inquiry=None, payload=None):
	return reschedule_school_admin_inquiry_data(inquiry=inquiry, payload=payload)


@frappe.whitelist()
def school_admin_get_conversion_sessions(inquiry=None, start_date=None, course=None, campus=None):
	return get_school_admin_conversion_sessions_data(
		inquiry=inquiry,
		start_date=start_date,
		course=course,
		campus=campus,
	)


@frappe.whitelist()
def school_admin_convert_inquiry(inquiry=None, course_session=None):
	return convert_school_admin_inquiry_data(inquiry=inquiry, course_session=course_session)


@frappe.whitelist()
def school_admin_get_invoices(status=None, customer=None, parent=None, student=None, source=None, limit=80):
	return get_school_admin_invoices_data(
		status=status,
		customer=customer,
		parent=parent,
		student=student,
		source=source,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_invoice(invoice=None):
	return get_school_admin_invoice_data(invoice=invoice)


@frappe.whitelist()
def school_admin_create_manual_invoice(payload=None):
	return create_school_admin_manual_invoice_data(payload=payload)


@frappe.whitelist()
def school_admin_update_draft_invoice(invoice=None, payload=None):
	return update_school_admin_draft_invoice_data(invoice=invoice, payload=payload)


@frappe.whitelist()
def school_admin_submit_invoice(invoice=None):
	return submit_school_admin_invoice_data(invoice=invoice)


@frappe.whitelist()
def school_admin_cancel_invoice(invoice=None, reason=None):
	return cancel_school_admin_invoice_data(invoice=invoice, reason=reason)


@frappe.whitelist()
def school_admin_get_enrollments(student=None, parent=None, course=None, term=None, enrollment_type=None, status=None, limit=80):
	return get_school_admin_enrollments_data(
		student=student,
		parent=parent,
		course=course,
		term=term,
		enrollment_type=enrollment_type,
		status=status,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_enrollment(enrollment=None):
	return get_school_admin_enrollment_data(enrollment=enrollment)


@frappe.whitelist()
def school_admin_get_weekly_timeslots(term=None, course=None, campus=None, teacher=None, status=None, limit=120):
	return get_school_admin_weekly_timeslots_data(
		term=term,
		course=course,
		campus=campus,
		teacher=teacher,
		status=status,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_weekly_timeslot(weekly_timeslot=None):
	return get_school_admin_weekly_timeslot_data(weekly_timeslot=weekly_timeslot)


@frappe.whitelist()
def school_admin_get_course_sessions(weekly_timeslot=None, term=None, course=None, campus=None, from_date=None, to_date=None, limit=160):
	return get_school_admin_course_sessions_data(
		weekly_timeslot=weekly_timeslot,
		term=term,
		course=course,
		campus=campus,
		from_date=from_date,
		to_date=to_date,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_course_session(course_session=None):
	return get_school_admin_course_session_data(course_session=course_session)
