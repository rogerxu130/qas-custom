import frappe

from qas_custom.services.school_admin import (
	add_school_admin_inquiry_note_data,
	adjust_school_admin_store_credit_data,
	cancel_school_admin_invoice_data,
	convert_school_admin_inquiry_data,
	create_school_admin_enrollment_data,
	create_school_admin_manual_invoice_data,
	create_school_admin_weekly_timeslot_data,
	end_school_admin_enrollment_data,
	generate_school_admin_course_sessions_data,
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
	get_school_admin_store_credit_data,
	get_school_admin_teacher_revenue_share_sessions_data,
	get_school_admin_vouchers_data,
	get_school_admin_weekly_timeslot_data,
	get_school_admin_weekly_timeslots_data,
	mark_school_admin_invoice_paid_data,
	mark_school_admin_inquiry_completed_data,
	mark_school_admin_inquiry_follow_up_data,
	mark_school_admin_inquiry_inactive_data,
	mark_school_admin_inquiry_no_show_data,
	reschedule_school_admin_inquiry_data,
	resend_school_admin_invoice_data,
	school_admin_global_search_data,
	submit_school_admin_invoice_data,
	transfer_school_admin_enrollment_data,
	update_school_admin_attendance_data,
	update_school_admin_draft_invoice_data,
	update_school_admin_enrollment_data,
	update_school_admin_inquiry_status_data,
	update_school_admin_voucher_data,
	update_school_admin_weekly_timeslot_data,
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
def school_admin_get_store_credit(parent=None, customer=None, limit=50):
	return get_school_admin_store_credit_data(parent=parent, customer=customer, limit=limit)


@frappe.whitelist()
def school_admin_adjust_store_credit(parent=None, customer=None, amount=0, reason=None, notes=None):
	return adjust_school_admin_store_credit_data(
		parent=parent,
		customer=customer,
		amount=amount,
		reason=reason,
		notes=notes,
	)


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
def school_admin_resend_invoice(invoice=None):
	return resend_school_admin_invoice_data(invoice=invoice)


@frappe.whitelist()
def school_admin_mark_invoice_paid(invoice=None, payload=None):
	return mark_school_admin_invoice_paid_data(invoice=invoice, payload=payload)


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
def school_admin_create_enrollment(payload=None):
	return create_school_admin_enrollment_data(payload=payload)


@frappe.whitelist()
def school_admin_update_enrollment(enrollment=None, payload=None):
	return update_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_transfer_enrollment(enrollment=None, payload=None):
	return transfer_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_end_enrollment(enrollment=None, payload=None):
	return end_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


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
def school_admin_create_weekly_timeslot(payload=None):
	return create_school_admin_weekly_timeslot_data(payload=payload)


@frappe.whitelist()
def school_admin_update_weekly_timeslot(weekly_timeslot=None, payload=None):
	return update_school_admin_weekly_timeslot_data(weekly_timeslot=weekly_timeslot, payload=payload)


@frappe.whitelist()
def school_admin_generate_course_sessions(weekly_timeslot=None, from_date=None, to_date=None):
	return generate_school_admin_course_sessions_data(weekly_timeslot=weekly_timeslot, from_date=from_date, to_date=to_date)


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


@frappe.whitelist()
def school_admin_update_attendance(attendance_entry=None, status=None, comments=None):
	return update_school_admin_attendance_data(attendance_entry=attendance_entry, status=status, comments=comments)


@frappe.whitelist()
def school_admin_get_vouchers(student=None, status=None, limit=120):
	return get_school_admin_vouchers_data(student=student, status=status, limit=limit)


@frappe.whitelist()
def school_admin_update_voucher(voucher=None, payload=None):
	return update_school_admin_voucher_data(voucher=voucher, payload=payload)


@frappe.whitelist()
def school_admin_get_teacher_revenue_share_sessions(
	from_date=None,
	to_date=None,
	teacher=None,
	campus=None,
	course=None,
	owned_only=1,
	limit=200,
):
	return get_school_admin_teacher_revenue_share_sessions_data(
		from_date=from_date,
		to_date=to_date,
		teacher=teacher,
		campus=campus,
		course=course,
		owned_only=owned_only,
		limit=limit,
	)
