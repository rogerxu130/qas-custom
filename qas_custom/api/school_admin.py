import frappe

from qas_custom.services.announcements import (
	archive_school_admin_announcement_data,
	get_school_admin_announcement_data,
	get_school_admin_announcements_data,
	publish_school_admin_announcement_data,
	save_school_admin_announcement_data,
)
from qas_custom.services.school_admin_import import (
	get_import_run_data,
	get_import_runs_data,
	get_operation_report_data,
	get_operation_reports_data,
	preview_enrollment_change_data,
	preview_enrollment_cancellation_import_data,
	preview_enrollment_import_data,
	preview_invoice_enrollment_reset_data,
	preview_parent_student_import_data,
	preview_store_credit_import_data,
	preview_trial_inquiry_import_data,
	run_enrollment_change_data,
	run_enrollment_cancellation_import_data,
	run_enrollment_import_data,
	run_invoice_enrollment_reset_data,
	run_parent_student_import_data,
	run_store_credit_import_data,
	run_trial_inquiry_import_data,
)
from qas_custom.services.school_admin import (
	update_school_admin_student_data,
	update_school_admin_parent_data,
	update_school_admin_course_data,
	set_school_admin_student_status_data,
	set_school_admin_parent_status_data,
	set_school_admin_course_status_data,
	get_school_admin_students_data,
	get_school_admin_parents_data,
	get_school_admin_courses_data,
	delete_school_admin_term_data,
	delete_school_admin_student_data,
	delete_school_admin_parent_data,
	delete_school_admin_course_data,
	create_school_admin_student_data,
	create_school_admin_parent_data,
	create_school_admin_course_data,
	add_school_admin_inquiry_note_data,
	adjust_school_admin_store_credit_data,
	activate_school_admin_enrollment_data,
	bulk_school_admin_invoice_action_data,
	cancel_school_admin_invoice_data,
	convert_school_admin_inquiry_data,
	create_school_admin_enrollment_data,
	create_school_admin_enrollment_attendance_data,
	create_school_admin_enrollment_invoice_data,
	create_school_admin_family_attendance_data,
	create_school_admin_family_invoice_data,
	create_school_admin_course_session_attendance_data,
	create_school_admin_manual_invoice_data,
	create_school_admin_term_invoices_data,
	create_school_admin_term_attendance_data,
	create_school_admin_term_data,
	create_school_admin_weekly_timeslot_data,
	delete_school_admin_draft_invoice_data,
	delete_school_admin_enrollment_data,
	delete_school_admin_setup_record_data,
	end_school_admin_enrollment_data,
	generate_school_admin_course_sessions_data,
	get_school_admin_conversion_sessions_data,
	get_school_admin_course_session_data,
	get_school_admin_course_sessions_data,
	get_school_admin_csrf_token_data,
	get_school_admin_bulk_invoice_submit_job_data,
	get_school_admin_dashboard_data,
	get_school_admin_enrollment_data,
	get_school_admin_enrollments_data,
	get_school_admin_family_data,
	get_school_admin_inquiries_data,
	get_school_admin_inquiry_data,
	get_school_admin_invoice_data,
	get_school_admin_invoices_data,
	get_school_admin_invoice_settings_data,
	get_school_admin_invoice_items_data,
	get_school_admin_leave_options_data,
	get_school_admin_me_data,
	get_school_admin_redeemable_sessions_data,
	get_school_admin_setup_records_data,
	get_school_admin_store_credit_data,
	get_school_admin_teacher_revenue_share_sessions_data,
	get_school_admin_term_data,
	get_school_admin_terms_data,
	get_school_admin_teacher_directory_data,
	get_school_admin_vouchers_data,
	get_school_admin_weekly_timeslot_data,
	get_school_admin_weekly_timeslots_data,
	mark_school_admin_invoice_paid_data,
	start_school_admin_bulk_invoice_submit_job_data,
	mark_school_admin_inquiry_completed_data,
	mark_school_admin_inquiry_follow_up_data,
	mark_school_admin_inquiry_inactive_data,
	mark_school_admin_inquiry_no_show_data,
	populate_school_admin_term_data,
	reschedule_school_admin_inquiry_data,
	redeem_school_admin_voucher_data,
	resend_school_admin_invoice_data,
	send_school_admin_trial_class_reminder_data,
	save_school_admin_setup_record_data,
	school_admin_global_search_data,
	submit_school_admin_leave_request_data,
	submit_school_admin_invoice_data,
	transfer_school_admin_enrollment_data,
	copy_school_admin_term_data,
	update_school_admin_attendance_data,
	update_school_admin_course_session_teacher_data,
	update_school_admin_draft_invoice_data,
	update_school_admin_enrollment_data,
	update_school_admin_inquiry_status_data,
	update_school_admin_invoice_settings_data,
	update_school_admin_voucher_data,
	update_school_admin_weekly_timeslot_data,
	change_school_admin_weekly_timeslot_teacher_data,
)


@frappe.whitelist()
def school_admin_get_me():
	return get_school_admin_me_data()


@frappe.whitelist()
def school_admin_get_csrf_token():
	return get_school_admin_csrf_token_data()


@frappe.whitelist()
def school_admin_get_teacher_directory(query=None, limit=300):
	return get_school_admin_teacher_directory_data(query=query, limit=limit)


@frappe.whitelist()
def school_admin_get_dashboard():
	return get_school_admin_dashboard_data()


@frappe.whitelist()
def school_admin_get_announcements(status=None, limit=80):
	return get_school_admin_announcements_data(status=status, limit=limit)


@frappe.whitelist()
def school_admin_get_announcement(announcement=None):
	return get_school_admin_announcement_data(announcement=announcement)


@frappe.whitelist()
def school_admin_save_announcement(announcement=None, payload=None):
	return save_school_admin_announcement_data(announcement=announcement, payload=payload)


@frappe.whitelist()
def school_admin_publish_announcement(announcement=None):
	return publish_school_admin_announcement_data(announcement=announcement)


@frappe.whitelist()
def school_admin_archive_announcement(announcement=None):
	return archive_school_admin_announcement_data(announcement=announcement)


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
def school_admin_preview_parent_student_import(payload=None):
	return preview_parent_student_import_data(payload=payload)


@frappe.whitelist()
def school_admin_run_parent_student_import(payload=None):
	return run_parent_student_import_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_store_credit_import(payload=None):
	return preview_store_credit_import_data(payload=payload)


@frappe.whitelist()
def school_admin_run_store_credit_import(payload=None):
	return run_store_credit_import_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_trial_inquiry_import(payload=None):
	return preview_trial_inquiry_import_data(payload=payload)


@frappe.whitelist()
def school_admin_run_trial_inquiry_import(payload=None):
	return run_trial_inquiry_import_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_enrollment_import(payload=None):
	return preview_enrollment_import_data(payload=payload)


@frappe.whitelist()
def school_admin_run_enrollment_import(payload=None):
	return run_enrollment_import_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_enrollment_cancellation_import(payload=None):
	return preview_enrollment_cancellation_import_data(payload=payload)


@frappe.whitelist()
def school_admin_run_enrollment_cancellation_import(payload=None):
	return run_enrollment_cancellation_import_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_enrollment_change(payload=None):
	return preview_enrollment_change_data(payload=payload)


@frappe.whitelist()
def school_admin_run_enrollment_change(payload=None):
	return run_enrollment_change_data(payload=payload)


@frappe.whitelist()
def school_admin_preview_invoice_enrollment_reset(payload=None):
	return preview_invoice_enrollment_reset_data(payload=payload)


@frappe.whitelist()
def school_admin_run_invoice_enrollment_reset(payload=None):
	return run_invoice_enrollment_reset_data(payload=payload)


@frappe.whitelist()
def school_admin_get_import_runs(import_type=None, limit=20):
	return get_import_runs_data(import_type=import_type, limit=limit)


@frappe.whitelist()
def school_admin_get_import_run(import_run=None):
	return get_import_run_data(import_run=import_run)


@frappe.whitelist()
def school_admin_get_operation_reports(report_type=None, source=None, limit=20):
	return get_operation_reports_data(report_type=report_type, source=source, limit=limit)


@frappe.whitelist()
def school_admin_get_operation_report(operation_report=None):
	return get_operation_report_data(operation_report=operation_report)


@frappe.whitelist()
def school_admin_get_parents(query=None, status=None, limit=120):
	return get_school_admin_parents_data(query=query, status=status, limit=limit)


@frappe.whitelist()
def school_admin_create_parent(payload=None):
	return create_school_admin_parent_data(payload=payload)


@frappe.whitelist()
def school_admin_update_parent(parent=None, payload=None):
	return update_school_admin_parent_data(parent=parent, payload=payload)


@frappe.whitelist()
def school_admin_set_parent_status(parent=None, status=None):
	return set_school_admin_parent_status_data(parent=parent, status=status)


@frappe.whitelist()
def school_admin_delete_parent(parent=None):
	return delete_school_admin_parent_data(parent=parent)


@frappe.whitelist()
def school_admin_get_students(parent=None, query=None, status=None, limit=120):
	return get_school_admin_students_data(parent=parent, query=query, status=status, limit=limit)


@frappe.whitelist()
def school_admin_create_student(payload=None):
	return create_school_admin_student_data(payload=payload)


@frappe.whitelist()
def school_admin_update_student(student=None, payload=None):
	return update_school_admin_student_data(student=student, payload=payload)


@frappe.whitelist()
def school_admin_set_student_status(student=None, status=None):
	return set_school_admin_student_status_data(student=student, status=status)


@frappe.whitelist()
def school_admin_delete_student(student=None):
	return delete_school_admin_student_data(student=student)


@frappe.whitelist()
def school_admin_get_courses(query=None, status=None, limit=120):
	return get_school_admin_courses_data(query=query, status=status, limit=limit)


@frappe.whitelist()
def school_admin_get_invoice_items(query=None, limit=120):
	return get_school_admin_invoice_items_data(query=query, limit=limit)


@frappe.whitelist()
def school_admin_create_course(payload=None):
	return create_school_admin_course_data(payload=payload)


@frappe.whitelist()
def school_admin_update_course(course=None, payload=None):
	return update_school_admin_course_data(course=course, payload=payload)


@frappe.whitelist()
def school_admin_set_course_status(course=None, status=None):
	return set_school_admin_course_status_data(course=course, status=status)


@frappe.whitelist()
def school_admin_delete_course(course=None):
	return delete_school_admin_course_data(course=course)


@frappe.whitelist()
def school_admin_get_invoice_settings():
	return get_school_admin_invoice_settings_data()


@frappe.whitelist()
def school_admin_update_invoice_settings(payload=None):
	return update_school_admin_invoice_settings_data(payload=payload)


@frappe.whitelist()
def school_admin_get_setup_records(record_type=None, query=None, status=None, limit=120):
	return get_school_admin_setup_records_data(record_type=record_type, query=query, status=status, limit=limit)


@frappe.whitelist()
def school_admin_save_setup_record(record_type=None, name=None, payload=None):
	return save_school_admin_setup_record_data(record_type=record_type, name=name, payload=payload)


@frappe.whitelist()
def school_admin_delete_setup_record(record_type=None, name=None):
	return delete_school_admin_setup_record_data(record_type=record_type, name=name)


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
def school_admin_get_inquiries(status=None, inquiry_type=None, campus=None, from_date=None, to_date=None, queue=None, query=None, limit=80):
	return get_school_admin_inquiries_data(
		status=status,
		inquiry_type=inquiry_type,
		campus=campus,
		from_date=from_date,
		to_date=to_date,
		queue=queue,
		query=query,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_inquiry(inquiry=None):
	return get_school_admin_inquiry_data(inquiry=inquiry)


@frappe.whitelist()
def school_admin_add_inquiry_note(inquiry=None, note=None):
	return add_school_admin_inquiry_note_data(inquiry=inquiry, note=note)


@frappe.whitelist()
def school_admin_send_trial_class_reminder(inquiry=None):
	return send_school_admin_trial_class_reminder_data(inquiry=inquiry)


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
def school_admin_delete_draft_invoice(invoice=None):
	return delete_school_admin_draft_invoice_data(invoice=invoice)


@frappe.whitelist()
def school_admin_submit_invoice(invoice=None, send_notifications=True):
	return submit_school_admin_invoice_data(invoice=invoice, send_notifications=send_notifications)


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
def school_admin_bulk_invoice_action(payload=None):
	return bulk_school_admin_invoice_action_data(payload=payload)


@frappe.whitelist()
def school_admin_start_bulk_invoice_submit_job(payload=None):
	return start_school_admin_bulk_invoice_submit_job_data(payload=payload)


@frappe.whitelist()
def school_admin_get_bulk_invoice_submit_job(job_id=None):
	return get_school_admin_bulk_invoice_submit_job_data(job_id=job_id)


@frappe.whitelist()
def school_admin_get_terms(status=None, limit=80):
	return get_school_admin_terms_data(status=status, limit=limit)


@frappe.whitelist()
def school_admin_get_term(term=None):
	return get_school_admin_term_data(term=term)


@frappe.whitelist()
def school_admin_create_term(payload=None):
	return create_school_admin_term_data(payload=payload)


@frappe.whitelist()
def school_admin_copy_term(payload=None):
	return copy_school_admin_term_data(payload=payload)


@frappe.whitelist()
def school_admin_delete_term(term=None):
	return delete_school_admin_term_data(term=term)


@frappe.whitelist()
def school_admin_populate_term(term=None, plan=None):
	return populate_school_admin_term_data(term=term or plan)


@frappe.whitelist()
def school_admin_get_enrollments(
	student=None,
	parent=None,
	course=None,
	term=None,
	enrollment_type=None,
	status=None,
	statuses=None,
	include_inactive_terms=0,
	limit=80,
):
	return get_school_admin_enrollments_data(
		student=student,
		parent=parent,
		course=course,
		term=term,
		enrollment_type=enrollment_type,
		status=status,
		statuses=statuses,
		include_inactive_terms=include_inactive_terms,
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
def school_admin_activate_enrollment(enrollment=None, payload=None):
	return activate_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_create_enrollment_attendance(enrollment=None, payload=None):
	return create_school_admin_enrollment_attendance_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_create_enrollment_invoice(enrollment=None, payload=None):
	return create_school_admin_enrollment_invoice_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_create_family_attendance(parent=None, customer=None, payload=None):
	return create_school_admin_family_attendance_data(parent=parent, customer=customer, payload=payload)


@frappe.whitelist()
def school_admin_create_family_invoice(parent=None, customer=None, payload=None):
	return create_school_admin_family_invoice_data(parent=parent, customer=customer, payload=payload)


@frappe.whitelist()
def school_admin_create_term_invoices(term=None, payload=None):
	return create_school_admin_term_invoices_data(term=term, payload=payload)


@frappe.whitelist()
def school_admin_create_term_attendance(term=None, payload=None):
	return create_school_admin_term_attendance_data(term=term, payload=payload)


@frappe.whitelist()
def school_admin_transfer_enrollment(enrollment=None, payload=None):
	return transfer_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_end_enrollment(enrollment=None, payload=None):
	return end_school_admin_enrollment_data(enrollment=enrollment, payload=payload)


@frappe.whitelist()
def school_admin_delete_enrollment(enrollment=None):
	return delete_school_admin_enrollment_data(enrollment=enrollment)


@frappe.whitelist()
def school_admin_get_weekly_timeslots(
	term=None,
	course=None,
	campus=None,
	teacher=None,
	status=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=120,
):
	return get_school_admin_weekly_timeslots_data(
		term=term,
		course=course,
		campus=campus,
		teacher=teacher,
		status=status,
		include_inactive_terms=include_inactive_terms,
		include_inactive_timeslots=include_inactive_timeslots,
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
def school_admin_get_course_sessions(
	weekly_timeslot=None,
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=160,
):
	return get_school_admin_course_sessions_data(
		weekly_timeslot=weekly_timeslot,
		term=term,
		course=course,
		campus=campus,
		from_date=from_date,
		to_date=to_date,
		include_inactive_terms=include_inactive_terms,
		include_inactive_timeslots=include_inactive_timeslots,
		limit=limit,
	)


@frappe.whitelist()
def school_admin_get_course_session(course_session=None):
	return get_school_admin_course_session_data(course_session=course_session)


@frappe.whitelist()
def school_admin_update_course_session_teacher(course_session=None, teacher=None, reset_override=0):
	return update_school_admin_course_session_teacher_data(
		course_session=course_session,
		teacher=teacher,
		reset_override=reset_override,
	)


@frappe.whitelist()
def school_admin_change_weekly_timeslot_teacher(weekly_timeslot=None, teacher=None, effective_date=None):
	return change_school_admin_weekly_timeslot_teacher_data(
		weekly_timeslot=weekly_timeslot,
		teacher=teacher,
		effective_date=effective_date,
	)


@frappe.whitelist()
def school_admin_update_attendance(attendance_entry=None, status=None, comments=None):
	return update_school_admin_attendance_data(attendance_entry=attendance_entry, status=status, comments=comments)


@frappe.whitelist()
def school_admin_create_course_session_attendance(course_session=None, payload=None):
	return create_school_admin_course_session_attendance_data(course_session=course_session, payload=payload)


@frappe.whitelist()
def school_admin_get_leave_options(parent=None, student=None):
    return get_school_admin_leave_options_data(parent=parent, student=student)


@frappe.whitelist()
def school_admin_submit_leave_request(parent=None, student=None, course_session=None, reason=None):
    return submit_school_admin_leave_request_data(parent=parent, student=student, course_session=course_session, reason=reason)


@frappe.whitelist()
def school_admin_get_redeemable_sessions(parent=None, voucher_id=None, student=None):
    return get_school_admin_redeemable_sessions_data(parent=parent, voucher_id=voucher_id, student=student)


@frappe.whitelist()
def school_admin_redeem_voucher(parent=None, voucher_id=None, session_id=None, student=None, reason=None):
    return redeem_school_admin_voucher_data(parent=parent, voucher_id=voucher_id, session_id=session_id, student=student, reason=reason)


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
