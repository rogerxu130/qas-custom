from __future__ import annotations

import frappe
from frappe import _

from frappe.utils import add_days, cint, escape_html, flt, getdate, now_datetime, today

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.modules.workflows.trial_conversion import (
	LINKABLE_ENROLLMENT_STATUSES,
	link_existing_enrollment_core,
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
	_create_payment_entry_for_invoice,
	_count_leave_attendance_rows,
	_course_session_sort_key,
	_document_payload,
	_get_course_session_rows,
	_get_school_admin_file_content,
	_get_school_admin_attendance_rows,
	_get_school_admin_session_content_rows,
	_get_timeslot_summary,
	_roster_course_session_attendance_rows,
	_visible_course_session_attendance_rows,
)
from qas_custom.modules.billing.store_credit import get_invoice_payable_amount, sync_invoice_store_credit_snapshot
from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.notifications import enqueue_parent_invoice_paid_receipt
from qas_custom.utils.environment import email_block_reason, payment_block_reason, payment_mutations_enabled, sendmail_or_skip
from qas_custom.services.teacher_directory import get_active_teacher_directory_data
from qas_custom.services.support_view import get_support_view_campus_admin_profile, reject_support_view_write


POST_VISIT_INQUIRY_STATUSES = (
	"Completed",
	"Follow-up",
	"Further Trial Booked",
	"No-show",
	"Converted",
	"Inactive",
)
CAMPUS_ADMIN_INQUIRY_RESULT_LIMIT = 200
CAMPUS_ADMIN_CURRENT_TERM_ENROLLMENT_RESULT_LIMIT = 50
CAMPUS_ADMIN_INQUIRY_SEARCH_FIELDS = (
	"name",
	"submitted_student_name",
	"student",
	"parent",
	"contact_name",
	"contact_email",
	"contact_phone",
)
CAMPUS_ADMIN_TRIAL_PAYMENT_METHODS = {"Cash", "EFTPOS", "Bank Transfer", "Other"}
CASH_MODE_OF_PAYMENT = "Cash"
EFTPOS_MODE_OF_PAYMENT = "EFTPOS"


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


def mark_campus_admin_trial_invoice_paid_data(invoice=None, payload=None):
	"""Settle one assigned-campus Trial Invoice in full and keep the payment audit trail."""
	reject_support_view_write()
	profile = _require_campus_admin_profile()
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))
	invoice = str(invoice or "").strip()
	if not invoice:
		frappe.throw(_("Trial Invoice is required."))
	payload = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	payment_method = str(payload.get("payment_method") or "").strip()
	if payment_method not in CAMPUS_ADMIN_TRIAL_PAYMENT_METHODS:
		frappe.throw(_("Select a valid payment method."))
	note = str(payload.get("note") or "").strip()

	inquiry_rows = frappe.get_all(
		"Inquiry",
		filters={
			"trial_invoice": invoice,
			"inquiry_type": "Trial Lesson",
			"campus": ["in", profile["campuses"]],
		},
		fields=["name", "campus", "student", "parent", "submitted_student_name", "contact_name", "contact_email"],
		limit=2,
	)
	if len(inquiry_rows) != 1:
		frappe.throw(_("This Trial Invoice is not available for your assigned campus."), frappe.PermissionError)

	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 1 or str(doc.get("status") or "").lower() == "cancelled":
		frappe.throw(_("Only submitted, active Trial Invoices can be paid."))
	amount = max(flt(get_invoice_payable_amount(doc)), 0)
	if amount <= 0.005:
		frappe.throw(_("This Trial Invoice has already been paid."))

	audit_note = "Campus Admin {0} recorded {1} payment for Trial Inquiry {2}.".format(
		frappe.session.user,
		payment_method,
		inquiry_rows[0].get("name"),
	)
	if note:
		audit_note = "{0}\nNote: {1}".format(audit_note, note)
	_ensure_campus_admin_trial_mode_of_payment(payment_method)
	payment_entry = _create_payment_entry_for_invoice(
		doc,
		amount=amount,
		mode_of_payment=payment_method,
		reference_no="Campus Admin payment {0}".format(now_datetime()),
		notes=audit_note,
	)
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Info",
		"reference_doctype": "Sales Invoice",
		"reference_name": doc.name,
		"content": "{0} Payment Entry: {1}".format(audit_note, payment_entry.name),
	}).insert(ignore_permissions=True)
	sync_invoice_store_credit_snapshot(doc.name)
	frappe.db.commit()
	receipt_notification = enqueue_parent_invoice_paid_receipt(
		frappe.get_doc("Sales Invoice", doc.name),
		payment_entry=payment_entry,
		source="campus_admin_mark_paid",
	)
	school_admin_notification = _enqueue_campus_admin_trial_payment_notification(
		invoice=doc.name,
		payment_entry=payment_entry.name,
		inquiry=inquiry_rows[0],
		amount=amount,
		payment_method=payment_method,
		note=note,
		campus_admin=frappe.session.user,
	)
	frappe.db.commit()
	return {
		"invoice": doc.name,
		"inquiry": inquiry_rows[0].get("name"),
		"campus": inquiry_rows[0].get("campus"),
		"payment_entry": payment_entry.name,
		"paid_amount": amount,
		"payment_method": payment_method,
		"note": note,
		"receipt_notification": receipt_notification,
		"school_admin_notification": school_admin_notification,
	}


def _enqueue_campus_admin_trial_payment_notification(*, invoice, payment_entry, inquiry, amount, payment_method, note, campus_admin):
	"""Queue the School Admin audit email after a Campus Admin settles a trial invoice."""
	job_id = "qas-campus-trial-payment-{0}".format(payment_entry)
	try:
		frappe.enqueue(
			"qas_custom.services.campus_admin.send_campus_admin_trial_payment_notification_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			deduplicate=True,
			job_id=job_id,
			invoice=invoice,
			payment_entry=payment_entry,
			inquiry=inquiry.get("name"),
			campus=inquiry.get("campus"),
			student=inquiry.get("submitted_student_name") or inquiry.get("student"),
			parent=inquiry.get("contact_name") or inquiry.get("parent"),
			amount=amount,
			payment_method=payment_method,
			note=note,
			campus_admin=campus_admin,
		)
		return {"queued": True, "job_id": job_id}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS Campus Admin trial payment notification queue failed: {0}".format(payment_entry))
		return {"queued": False, "reason": "School Admin notification could not be queued."}


def send_campus_admin_trial_payment_notification_job(
	invoice=None,
	payment_entry=None,
	inquiry=None,
	campus=None,
	student=None,
	parent=None,
	amount=None,
	payment_method=None,
	note=None,
	campus_admin=None,
):
	"""Deliver a School Admin audit email for one Campus Admin trial payment."""
	from qas_custom.services.maintenance import _get_school_admin_emails

	recipients = _get_school_admin_emails()
	if not recipients:
		return {"sent": False, "reason": "No active School Admin email recipients were found."}
	if not invoice or not payment_entry:
		return {"sent": False, "reason": "Invoice and Payment Entry are required."}
	settings = get_invoice_settings()
	subject = _("Campus Admin recorded Trial payment – {0} – AUD {1:.2f}").format(campus or "Campus", flt(amount))
	message = """
		<p><strong>{school}</strong></p>
		<p>A Campus Admin has recorded a Trial Invoice payment.</p>
		<ul>
			<li><strong>Campus:</strong> {campus}</li>
			<li><strong>Trial inquiry:</strong> {inquiry}</li>
			<li><strong>Student:</strong> {student}</li>
			<li><strong>Parent:</strong> {parent}</li>
			<li><strong>Invoice:</strong> {invoice}</li>
			<li><strong>Payment Entry:</strong> {payment_entry}</li>
			<li><strong>Amount:</strong> AUD {amount:.2f}</li>
			<li><strong>Payment method:</strong> {payment_method}</li>
			<li><strong>Recorded by:</strong> {campus_admin}</li>
			<li><strong>Note:</strong> {note}</li>
		</ul>
	""".format(
		school=escape_html(settings.get("school_name") or "Queensland Art School"),
		campus=escape_html(campus or "-"), inquiry=escape_html(inquiry or "-"),
		student=escape_html(student or "-"), parent=escape_html(parent or "-"),
		invoice=escape_html(invoice), payment_entry=escape_html(payment_entry), amount=flt(amount),
		payment_method=escape_html(payment_method or "-"), campus_admin=escape_html(campus_admin or "-"),
		note=escape_html(note or "-"),
	)
	try:
		result = sendmail_or_skip(
			action="campus_admin_trial_payment_recorded",
			recipients=recipients,
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=invoice,
			delayed=False,
		)
		if result and result.get("skipped"):
			return {"sent": False, "skipped": True, "reason": result.get("reason") or email_block_reason()}
		return {"sent": True, "recipients": recipients}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS Campus Admin trial payment notification failed: {0}".format(payment_entry))
		return {"sent": False, "reason": "School Admin notification email failed."}


def _ensure_campus_admin_trial_mode_of_payment(payment_method):
	"""Ensure every Campus Admin Trial payment label has an ERPNext payment mode."""
	payment_method = str(payment_method or "").strip()
	if payment_method not in CAMPUS_ADMIN_TRIAL_PAYMENT_METHODS:
		frappe.throw(_("Select a valid payment method."))
	if frappe.db.exists("Mode of Payment", payment_method):
		return payment_method
	if not frappe.db.exists("Mode of Payment", CASH_MODE_OF_PAYMENT):
		frappe.throw(
			_("{0} cannot be set up because Cash is not configured.").format(payment_method)
		)

	cash_mode = frappe.get_doc("Mode of Payment", CASH_MODE_OF_PAYMENT)
	accounts = [
		{
			"company": row.get("company"),
			"default_account": row.get("default_account"),
		}
		for row in cash_mode.get("accounts", [])
		if row.get("company") and row.get("default_account")
	]
	if not accounts:
		frappe.throw(
			_("{0} cannot be set up because Cash has no receiving account configuration.").format(payment_method)
		)

	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		if frappe.db.exists("Mode of Payment", payment_method):
			return payment_method
		mode = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": payment_method,
			"type": cash_mode.get("type") or "General",
			"accounts": accounts,
		})
		mode.insert(ignore_permissions=True)
		return mode.name
	finally:
		frappe.set_user(original_user)


def _ensure_eftpos_mode_of_payment():
	"""Compatibility wrapper for the original EFTPOS provisioning helper."""
	return _ensure_campus_admin_trial_mode_of_payment(EFTPOS_MODE_OF_PAYMENT)


def get_campus_admin_teacher_directory_data(query=None, limit=300):
	_require_campus_admin_profile()
	return get_active_teacher_directory_data(query=query, limit=limit)


def get_campus_admin_current_term_enrollments_data(query=None, limit=50):
	profile = _require_campus_admin_profile()
	term_result = _get_campus_admin_current_active_term()
	if term_result["state"] != "ready":
		return {
			"items": [],
			"term": None,
			"searched": False,
			**term_result,
		}

	term = term_result["term"]
	query = str(query or "").strip()
	if not query:
		return {
			"items": [],
			"term": term,
			"searched": False,
			"state": "ready",
		}

	page_limit = min(max(cint(limit or CAMPUS_ADMIN_CURRENT_TERM_ENROLLMENT_RESULT_LIMIT), 1), CAMPUS_ADMIN_CURRENT_TERM_ENROLLMENT_RESULT_LIMIT)
	student_rows = _get_campus_admin_enrollment_search_students(query)
	if not student_rows:
		return {
			"items": [],
			"term": term,
			"searched": True,
			"state": "ready",
		}

	timeslot_rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": term, "campus": ["in", profile["campuses"]]},
		fields=["name", "course", "campus", "day_of_week", "start_time", "end_time"],
		limit_page_length=0,
	)
	if not timeslot_rows:
		return {
			"items": [],
			"term": term,
			"searched": True,
			"state": "ready",
		}

	timeslot_map = {row.get("name"): row for row in timeslot_rows if row.get("name")}
	enrollments = frappe.get_all(
		"Enrollment",
		filters={
			"term": term,
			"student": ["in", [row["name"] for row in student_rows]],
			"weekly_timeslot": ["in", list(timeslot_map)],
		},
		fields=["name", "student", "term", "course", "weekly_timeslot", "enrollment_type", "status"],
		limit_page_length=page_limit * 4,
	)
	student_map = {row["name"]: row for row in student_rows}
	items = []
	for enrollment in enrollments:
		timeslot = timeslot_map.get(enrollment.get("weekly_timeslot"))
		student = student_map.get(enrollment.get("student"))
		if not timeslot or not student:
			continue
		items.append(
			{
				"id": enrollment.get("name"),
				"name": enrollment.get("name"),
				"student": enrollment.get("student"),
				"student_name": student.get("student_name") or student.get("name"),
				"term": term,
				"course": enrollment.get("course") or timeslot.get("course"),
				"campus": timeslot.get("campus"),
				"day_of_week": timeslot.get("day_of_week"),
				"start_time": timeslot.get("start_time"),
				"end_time": timeslot.get("end_time"),
				"enrollment_type": enrollment.get("enrollment_type"),
				"status": enrollment.get("status"),
			}
		)

	items.sort(key=_campus_admin_current_term_enrollment_sort_key)
	return {
		"items": items[:page_limit],
		"term": term,
		"searched": True,
		"state": "ready",
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
	_enrich_trial_payment_status(attendance_rows, profile["campuses"])
	attending_rows = _visible_course_session_attendance_rows(attendance_rows)
	payload["attendance"] = _roster_course_session_attendance_rows(attendance_rows)
	payload["student_count"] = len(attending_rows)
	payload["trial_count"] = sum(1 for row in attending_rows if row.get("source_doctype") == "Inquiry")
	payload["leave_count"] = _count_leave_attendance_rows(attendance_rows)
	timeslot_teacher = (payload.get("weekly_timeslot_detail") or {}).get("teacher")
	payload["teacher"] = payload.get("teacher_override") or timeslot_teacher
	_attach_campus_admin_teacher_labels([payload])
	payload["teacher_assignment_source"] = "Session override" if payload.get("teacher_override") else "Weekly timeslot"
	payload["class_content"] = _get_school_admin_session_content_rows(
		course_session,
		photo_method="qas_custom.api.campus_admin.campus_admin_get_course_session_photo",
		video_method="qas_custom.api.campus_admin.campus_admin_get_course_session_video",
	)
	return payload


def _enrich_trial_payment_status(attendance_rows, allowed_campuses=None):
	trial_rows = [row for row in attendance_rows if row.get("source_doctype") == "Inquiry"]
	if not trial_rows:
		return attendance_rows

	for row in trial_rows:
		row["trial_invoice"] = ""
		row["trial_invoice_active"] = False
		row["trial_invoice_outstanding_amount"] = 0
		row["trial_payment_status"] = "needs_front_desk"

	inquiry_names = sorted({row.get("source_document") for row in trial_rows if row.get("source_document")})
	if not inquiry_names:
		return attendance_rows

	inquiry_filters = {"name": ["in", inquiry_names]}
	if allowed_campuses:
		inquiry_filters["campus"] = ["in", list(allowed_campuses)]
	inquiry_rows = frappe.get_all(
		"Inquiry",
		filters=inquiry_filters,
		fields=["name", "trial_invoice"],
		limit_page_length=0,
	)
	invoice_by_inquiry = {
		row.get("name"): row.get("trial_invoice")
		for row in inquiry_rows
		if row.get("name")
	}

	missing_direct_links = sorted(
		inquiry
		for inquiry in inquiry_names
		if not invoice_by_inquiry.get(inquiry)
	)
	if missing_direct_links:
		fallback_rows = frappe.get_all(
			"Sales Invoice",
			filters={
				"source_doctype": "Inquiry",
				"source_document": ["in", missing_direct_links],
			},
			fields=["name", "source_document", "creation"],
			order_by="creation asc",
			limit_page_length=0,
		)
		for row in fallback_rows:
			inquiry = row.get("source_document")
			if inquiry and row.get("name") and not invoice_by_inquiry.get(inquiry):
				invoice_by_inquiry[inquiry] = row.get("name")

	invoice_names = sorted({invoice for invoice in invoice_by_inquiry.values() if invoice})
	invoice_map = {}
	if invoice_names:
		invoice_rows = frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", invoice_names]},
			fields=["name", "docstatus", "status", "outstanding_amount"],
			limit_page_length=0,
		)
		invoice_map = {row.get("name"): row for row in invoice_rows if row.get("name")}

	for row in trial_rows:
		invoice_name = invoice_by_inquiry.get(row.get("source_document"))
		invoice = invoice_map.get(invoice_name)
		row["trial_invoice"] = invoice_name or ""
		if not invoice or cint(invoice.get("docstatus")) != 1 or str(invoice.get("status") or "").lower() == "cancelled":
			continue

		outstanding_amount = max(flt(invoice.get("outstanding_amount") or 0), 0)
		row["trial_invoice_active"] = True
		row["trial_invoice_outstanding_amount"] = outstanding_amount
		row["trial_payment_status"] = "outstanding" if outstanding_amount > 0.005 else "paid"

	return attendance_rows


def get_campus_admin_session_photo_content_data(course_session=None, photo_post=None, photo_idx=None):
	profile = _require_campus_admin_profile()
	if not course_session or not photo_post:
		frappe.throw(_("Course session and photo post are required."))
	_get_campus_admin_course_session_access(course_session, profile["campuses"])

	photo_post_doc = frappe.get_doc("Session Photo Post", photo_post)
	if photo_post_doc.get("course_session") != course_session or photo_post_doc.get("status") != "Published":
		raise frappe.PermissionError

	target_idx = cint(photo_idx)
	if target_idx <= 0:
		raise frappe.PermissionError
	photo_row = next((row for row in photo_post_doc.photos or [] if cint(row.idx) == target_idx), None)
	if not photo_row or not getattr(photo_row, "image", None):
		raise frappe.DoesNotExistError
	return _get_school_admin_file_content(photo_row.image)


def get_campus_admin_session_video_content_data(course_session=None, video_post=None):
	profile = _require_campus_admin_profile()
	if not course_session or not video_post:
		frappe.throw(_("Course session and video post are required."))
	_get_campus_admin_course_session_access(course_session, profile["campuses"])

	video_post_doc = frappe.get_doc("Session Video Post", video_post)
	if video_post_doc.get("course_session") != course_session or video_post_doc.get("status") != "Published":
		raise frappe.PermissionError
	if not video_post_doc.get("video"):
		raise frappe.DoesNotExistError

	payload = _get_school_admin_file_content(
		video_post_doc.get("video"),
		fallback_filename=video_post_doc.get("file_name"),
		fallback_content_type=video_post_doc.get("mime_type"),
	)
	payload["display_content_as"] = "inline"
	return payload


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


def mark_campus_admin_inquiry_further_trial_booked_data(inquiry=None):
	reject_support_view_write()
	_require_inquiry_access(inquiry)
	return mark_inquiry_status_core(inquiry, "Further Trial Booked", actor=frappe.session.user)


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


def get_campus_admin_linkable_enrollments_data(inquiry=None):
	profile = _require_inquiry_access(inquiry)
	inquiry_doc, inquiry_term = _get_inquiry_link_context(
		inquiry,
		allowed_statuses={"Completed", "Follow-up"},
	)
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters={
			"campus": ["in", profile["campuses"]],
			"term": inquiry_term,
		},
		fields=[
			"name",
			"term",
			"course",
			"campus",
			"classroom",
			"teacher",
			"day_of_week",
			"start_time",
			"end_time",
		],
		order_by="campus asc, course asc, day_of_week asc, start_time asc",
		limit_page_length=0,
	)
	if not timeslots:
		return {"items": [], "term": inquiry_term}

	timeslot_map = {row.name: row for row in timeslots}
	enrollments = frappe.get_all(
		"Enrollment",
		filters={
			"student": inquiry_doc.student,
			"term": inquiry_term,
			"status": ["in", sorted(LINKABLE_ENROLLMENT_STATUSES)],
			"weekly_timeslot": ["in", list(timeslot_map)],
		},
		fields=[
			"name",
			"student",
			"parent",
			"term",
			"course",
			"weekly_timeslot",
			"status",
			"invoice",
			"source_inquiry",
		],
		order_by="status asc, course asc, modified desc",
		limit_page_length=0,
	)
	if not enrollments:
		return {"items": [], "term": inquiry_term}

	enrollment_names = [row.name for row in enrollments]
	conflicting_rows = frappe.get_all(
		"Inquiry",
		filters={
			"name": ["!=", inquiry_doc.name],
			"status": "Converted",
			"converted_enrollment": ["in", enrollment_names],
		},
		fields=["name", "converted_enrollment"],
		limit_page_length=0,
	)
	conflicting_enrollments = {row.converted_enrollment for row in conflicting_rows if row.converted_enrollment}

	items = []
	for enrollment in enrollments:
		if enrollment.get("source_inquiry") and enrollment.source_inquiry != inquiry_doc.name:
			continue
		if enrollment.name in conflicting_enrollments:
			continue
		if (
			enrollment.get("parent")
			and inquiry_doc.get("parent")
			and enrollment.parent != inquiry_doc.parent
		):
			continue
		timeslot = timeslot_map.get(enrollment.weekly_timeslot)
		if not timeslot:
			continue
		items.append(_campus_admin_linkable_enrollment_payload(enrollment, timeslot))

	return {"items": items, "term": inquiry_term}


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


def link_campus_admin_inquiry_enrollment_data(inquiry=None, enrollment=None):
	reject_support_view_write()
	profile = _require_inquiry_access(inquiry)
	_validate_linkable_enrollment_access(inquiry, enrollment, profile)
	return link_existing_enrollment_core(
		inquiry,
		enrollment,
		actor=frappe.session.user,
		operator_label=_("Campus Admin"),
	)


def _get_campus_admin_current_active_term():
	rows = frappe.get_all(
		"Term",
		filters={"status": "Active"},
		fields=["name"],
		order_by="start_date desc, name desc",
		limit_page_length=2,
	)
	if not rows:
		return {
			"state": "no_active_term",
			"message": _("No active term is configured."),
		}
	if len(rows) > 1:
		return {
			"state": "multiple_active_terms",
			"message": _("Multiple active terms are configured. Ask School Admin to keep only one active term."),
		}
	return {"state": "ready", "term": rows[0].get("name")}


def _get_campus_admin_enrollment_search_students(query):
	pattern = f"%{query}%"
	rows = frappe.get_all(
		"Student",
		or_filters=[
			["Student", "name", "like", pattern],
			["Student", "student_name", "like", pattern],
		],
		fields=["name", "student_name"],
		limit_page_length=200,
	)
	items = [dict(row) for row in rows if row.get("name")]
	query_key = query.casefold()

	def rank(row):
		name = str(row.get("student_name") or "").casefold()
		identifier = str(row.get("name") or "").casefold()
		if name == query_key or identifier == query_key:
			return 0
		if name.startswith(query_key) or identifier.startswith(query_key):
			return 1
		return 2

	return sorted(items, key=lambda row: (rank(row), str(row.get("student_name") or row.get("name")).casefold(), row["name"]))


def _campus_admin_current_term_enrollment_sort_key(item):
	return (
		str(item.get("student_name") or item.get("student") or "").casefold(),
		str(item.get("day_of_week") or ""),
		str(item.get("start_time") or ""),
		str(item.get("name") or ""),
	)


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


def _get_inquiry_link_context(inquiry, allowed_statuses=None):
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Only Trial Lesson inquiries can be converted."))
	if allowed_statuses and inquiry_doc.status not in allowed_statuses:
		frappe.throw(_("Only Completed or Follow-up inquiries can be linked to an existing Enrollment."))
	if not inquiry_doc.get("student"):
		frappe.throw(_("Student is required before converting a Trial Lesson Inquiry."))
	if not inquiry_doc.get("course_session"):
		frappe.throw(_("The Trial Lesson Inquiry does not have a Course Session."))

	session = frappe.db.get_value(
		"Course Sessions",
		inquiry_doc.course_session,
		["weekly_timeslot"],
		as_dict=True,
	)
	if not session or not session.get("weekly_timeslot"):
		frappe.throw(_("The Trial Lesson Course Session was not found."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.weekly_timeslot,
		["term"],
		as_dict=True,
	)
	if not timeslot or not timeslot.get("term"):
		frappe.throw(_("The Trial Lesson Course Session is missing a Term."))
	return inquiry_doc, timeslot.term


def _validate_linkable_enrollment_access(inquiry, enrollment, profile):
	if not enrollment:
		frappe.throw(_("Enrollment is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	idempotent_link = (
		inquiry_doc.status == "Converted"
		and inquiry_doc.get("converted_enrollment") == enrollment
	)
	inquiry_doc, inquiry_term = _get_inquiry_link_context(
		inquiry,
		allowed_statuses={"Completed", "Follow-up", "Converted"},
	)

	enrollment_doc = frappe.get_doc("Enrollment", enrollment)
	if not enrollment_doc.get("weekly_timeslot"):
		frappe.throw(_("The existing Enrollment does not have a Weekly Timeslot."))

	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		enrollment_doc.weekly_timeslot,
		["campus", "term"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("The existing Enrollment Weekly Timeslot was not found."))
	if timeslot.get("campus") not in profile["campuses"]:
		frappe.throw(_("You do not have access to the selected Enrollment campus."), frappe.PermissionError)
	if idempotent_link:
		return
	if enrollment_doc.get("status") not in LINKABLE_ENROLLMENT_STATUSES:
		frappe.throw(_("The existing Enrollment must be Planned or Active."))
	if enrollment_doc.get("student") != inquiry_doc.student:
		frappe.throw(_("The existing Enrollment must belong to the same Student as the Inquiry."))
	if enrollment_doc.get("term") != inquiry_term:
		frappe.throw(_("The existing Enrollment must belong to the same Term as the Inquiry."))
	if (
		enrollment_doc.get("parent")
		and inquiry_doc.get("parent")
		and enrollment_doc.parent != inquiry_doc.parent
	):
		frappe.throw(_("The existing Enrollment must belong to the same Parent as the Inquiry."))
	if timeslot.get("term") != inquiry_term:
		frappe.throw(_("The existing Enrollment class must belong to the same Term as the Inquiry."))


def _campus_admin_linkable_enrollment_payload(enrollment, timeslot):
	parts = [
		enrollment.name,
		enrollment.get("course") or timeslot.get("course"),
		timeslot.get("campus"),
		timeslot.get("day_of_week"),
		_format_linkable_enrollment_time(timeslot.get("start_time")),
		enrollment.get("term"),
		enrollment.get("status"),
		_("Invoice {0}").format(enrollment.invoice) if enrollment.get("invoice") else _("No invoice"),
	]
	return {
		"id": enrollment.name,
		"name": enrollment.name,
		"student": enrollment.get("student"),
		"parent": enrollment.get("parent"),
		"term": enrollment.get("term"),
		"course": enrollment.get("course") or timeslot.get("course"),
		"weekly_timeslot": enrollment.get("weekly_timeslot"),
		"status": enrollment.get("status"),
		"invoice": enrollment.get("invoice"),
		"campus": timeslot.get("campus"),
		"classroom": timeslot.get("classroom"),
		"teacher": timeslot.get("teacher"),
		"day_of_week": timeslot.get("day_of_week"),
		"start_time": timeslot.get("start_time"),
		"end_time": timeslot.get("end_time"),
		"label": " · ".join(str(part) for part in parts if part),
	}


def _format_linkable_enrollment_time(value):
	if not value:
		return None
	value = str(value)
	return value[:5] if len(value) >= 5 else value


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
