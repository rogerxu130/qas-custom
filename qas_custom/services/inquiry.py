from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import get_time, getdate, now_datetime

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	mark_inquiry_inactive_core,
)
from qas_custom.modules.attendance.commands import (
	cancel_trial_inquiry_attendance_entries,
	ensure_trial_inquiry_attendance_entry,
	remove_trial_inquiry_attendance_entries,
)
from qas_custom.modules.notifications.commands import get_trial_class_reminder_summary, send_trial_class_reminder
from qas_custom.utils.environment import sendmail_or_skip


INQUIRY_TYPES = {"Trial Lesson", "School Visit"}
ADMIN_ROLES = {"System Manager", "School Admin"}
NEEDS_REVIEW_STATUS = "Needs Review"
DAY_ABBREVIATIONS = {
	"mon": "Monday",
	"monday": "Monday",
	"tue": "Tuesday",
	"tues": "Tuesday",
	"tuesday": "Tuesday",
	"wed": "Wednesday",
	"wednesday": "Wednesday",
	"thu": "Thursday",
	"thur": "Thursday",
	"thurs": "Thursday",
	"thursday": "Thursday",
	"fri": "Friday",
	"friday": "Friday",
	"sat": "Saturday",
	"saturday": "Saturday",
	"sun": "Sunday",
	"sunday": "Sunday",
}


def create_inquiry_data(payload=None, source="Manual"):
	_require_admin()
	payload = _get_payload(payload)
	return create_inquiry_core(payload, source=source, actor=frappe.session.user)


def create_inquiry_webhook_data(payload=None):
	payload = _get_payload(payload)
	_validate_webhook_token(payload)
	normalized = _normalize_webhook_payload(payload)
	return create_inquiry_core(normalized, source=normalized.get("source") or "Webhook", actor=None)


def get_inquiry_data(inquiry=None):
	_require_admin()
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	return build_inquiry_detail(inquiry)


def send_trial_class_reminder_core(inquiry=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	send_trial_class_reminder(inquiry_doc)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def reschedule_inquiry_data(inquiry=None, payload=None):
	_require_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	return reschedule_inquiry_core(inquiry, payload, actor=frappe.session.user)


def assign_inquiry_course_session_data(inquiry=None, course_session=None):
	_require_admin()
	payload = _get_payload()
	inquiry = inquiry or payload.get("inquiry")
	course_session = course_session or payload.get("course_session")
	return assign_inquiry_course_session_core(inquiry, course_session, status="Booked")


def mark_inquiry_completed_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "Completed", actor=frappe.session.user)


def mark_inquiry_no_show_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "No-show", actor=frappe.session.user)


def mark_inquiry_cancelled_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "Cancelled", actor=frappe.session.user)


def mark_inquiry_follow_up_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "Follow-up", actor=frappe.session.user)


def convert_inquiry_data(inquiry=None, course_session=None, payload=None):
	_require_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	course_session = course_session or payload.get("course_session")
	return convert_inquiry_to_full_term_core(inquiry, course_session, actor=frappe.session.user)


def mark_inquiry_inactive_data(inquiry=None, inactive_reason=None, payload=None):
	_require_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	inactive_reason = inactive_reason if inactive_reason is not None else payload.get("inactive_reason")
	return mark_inquiry_inactive_core(inquiry, inactive_reason, actor=frappe.session.user)


def add_inquiry_note_data(inquiry=None, note=None):
	_require_admin()
	payload = _get_payload()
	inquiry = inquiry or payload.get("inquiry")
	note = note if note is not None else payload.get("note")
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)


def create_inquiry_core(payload: dict, source="Manual", actor=None):
	payload = _normalize_inquiry_payload(payload)
	inquiry_type = payload.get("inquiry_type")
	if inquiry_type not in INQUIRY_TYPES:
		frappe.throw(_("Inquiry type must be Trial Lesson or School Visit."))

	parent = _resolve_parent(payload, inquiry_type)
	student = _resolve_student(payload, parent, inquiry_type)
	session_context = None
	appointment_context = None
	review_reason = None
	if inquiry_type == "Trial Lesson":
		session_context, review_reason = _resolve_trial_session_context(payload, inquiry_type)
	else:
		appointment_context, review_reason = _resolve_school_visit_context(payload)

	if inquiry_type == "Trial Lesson" and session_context and not student:
		frappe.throw(_("A linked student is required before booking a trial lesson into a course session."))

	current_date, current_time = _get_requested_trial_datetime(payload)
	inquiry_doc = frappe.new_doc("Inquiry")
	inquiry_doc.inquiry_type = inquiry_type
	inquiry_doc.source = source or payload.get("source") or "Manual"
	inquiry_doc.status = _get_initial_inquiry_status(session_context or appointment_context, review_reason)
	inquiry_doc.campus = (
		session_context.get("campus")
		if session_context
		else appointment_context.get("campus")
		if appointment_context
		else payload.get("campus")
		if inquiry_type == "Trial Lesson"
		else None
	)
	inquiry_doc.parent = parent
	inquiry_doc.student = student
	inquiry_doc.contact_name = payload.get("contact_name") or payload.get("parent_name")
	inquiry_doc.contact_phone = payload.get("contact_phone") or payload.get("phone")
	inquiry_doc.contact_email = payload.get("contact_email") or payload.get("email")
	inquiry_doc.preferred_course = (
		session_context["timeslot"].get("course")
		if session_context
		else _resolve_course(payload.get("preferred_course") or payload.get("course"))
	)
	inquiry_doc.course_session = session_context["session"].get("name") if session_context else payload.get("course_session")
	inquiry_doc.submitted_form_name = payload.get("submitted_form_name")
	inquiry_doc.submitted_student_name = payload.get("submitted_student_name") or payload.get("student_name")
	inquiry_doc.submitted_student_dob = payload.get("submitted_student_dob") or payload.get("date_of_birth")
	inquiry_doc.submitted_class_session = payload.get("submitted_class_session")
	inquiry_doc.submitted_trial_date = payload.get("submitted_trial_date")
	inquiry_doc.referral_source = payload.get("referral_source")
	inquiry_doc.referral_detail = payload.get("referral_detail")
	if session_context:
		_apply_session_to_inquiry(inquiry_doc, session_context)
	elif appointment_context:
		inquiry_doc.current_appointment_date = appointment_context.get("appointment_date")
		inquiry_doc.current_appointment_time = appointment_context.get("appointment_time")
		inquiry_doc.review_reason = review_reason
	elif review_reason:
		if current_date:
			inquiry_doc.current_appointment_date = current_date
			inquiry_doc.current_appointment_time = current_time
		inquiry_doc.review_reason = review_reason
	inquiry_doc.confirmation_status = "Pending" if session_context or (appointment_context and not review_reason) else "Not Required"
	inquiry_doc.reminder_status = "Not Required"
	inquiry_doc.flags.ignore_permissions = True
	inquiry_doc.insert()

	if review_reason:
		_send_needs_review_alert(inquiry_doc, review_reason)

	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def reschedule_inquiry_core(inquiry: str | None, payload: dict, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))

	payload = _normalize_inquiry_payload(payload)
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status in {"Cancelled", "Completed", "Converted", "Inactive"}:
		frappe.throw(_("This inquiry cannot be rescheduled from its current status."))

	if inquiry_doc.inquiry_type == "Trial Lesson":
		course_session = payload.get("course_session")
		if not course_session:
			frappe.throw(_("Course session is required for a trial lesson."))
		return assign_inquiry_course_session_core(inquiry_doc.name, course_session, status="Rescheduled")

	appointment_date, appointment_time = _parse_appointment_datetime(payload)
	if not appointment_date:
		frappe.throw(_("Appointment date is required for a school visit."))
	if payload.get("campus"):
		inquiry_doc.campus = payload.get("campus")
	inquiry_doc.current_appointment_date = appointment_date
	inquiry_doc.current_appointment_time = appointment_time
	inquiry_doc.status = "Rescheduled"
	inquiry_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def assign_inquiry_course_session_core(inquiry: str | None, course_session: str | None, status="Booked"):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	if not course_session:
		frappe.throw(_("Course session is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Course session can only be assigned to a trial lesson inquiry."))
	if inquiry_doc.status in {"Cancelled", "Completed", "Converted", "Inactive"}:
		frappe.throw(_("This inquiry cannot be assigned from its current status."))
	inquiry_doc.course_session = course_session
	inquiry_doc.status = status or "Booked"
	inquiry_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def mark_inquiry_status_core(inquiry: str | None, status: str, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	if status not in {"Cancelled", "Completed", "No-show", "Follow-up"}:
		frappe.throw(_("Unsupported inquiry status."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status in {"Converted", "Inactive"}:
		frappe.throw(_("This inquiry cannot be updated from its current status."))
	if status == "Cancelled" and inquiry_doc.status in {"Completed", "Converted", "Inactive"}:
		frappe.throw(_("A completed inquiry cannot be cancelled."))
	if status == "Follow-up" and inquiry_doc.status != "Completed":
		frappe.throw(_("Follow-up can only be started after the trial lesson is completed."))
	if status in {"Completed", "No-show"} and inquiry_doc.status == "Cancelled":
		frappe.throw(_("A cancelled inquiry cannot be marked as attended or no-show."))
	inquiry_doc.status = status
	inquiry_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def add_inquiry_note_core(inquiry: str | None, note: str | None, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Note is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	note_doc = frappe.new_doc("Inquiry Note")
	note_doc.inquiry = inquiry_doc.name
	note_doc.student = inquiry_doc.student
	note_doc.note = note
	note_doc.author = actor or frappe.session.user
	note_doc.edited_at = now_datetime()
	if note_doc.meta.has_field("note_type"):
		note_doc.note_type = "Manual"
	note_doc.flags.ignore_permissions = True
	note_doc.insert()
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def build_inquiry_detail(inquiry: str):
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	reminder = get_trial_class_reminder_summary(inquiry_doc.name)
	if inquiry_doc.reminder_status:
		if not reminder:
			reminder = {"status": inquiry_doc.reminder_status}
		elif reminder.get("status") == "Logged":
			reminder["status"] = inquiry_doc.reminder_status
	return {
		"inquiry": _build_inquiry_payload(inquiry_doc),
		"notes": _get_note_payloads(inquiry_doc.name),
		"reminder": reminder,
	}


def build_inquiry_summary(inquiry_doc_or_name):
	inquiry_doc = (
		frappe.get_doc("Inquiry", inquiry_doc_or_name)
		if isinstance(inquiry_doc_or_name, str)
		else inquiry_doc_or_name
	)
	return _build_inquiry_payload(inquiry_doc)


def _require_admin():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can manage inquiries."), frappe.PermissionError)


def _normalize_inquiry_payload(payload: dict):
	payload = payload or {}
	normalized = dict(payload)
	aliases = {
		"type": "inquiry_type",
		"request_type": "inquiry_type",
		"name": "contact_name",
		"parent_full_name": "parent_name",
		"mobile": "contact_phone",
		"phone_number": "contact_phone",
		"email_address": "contact_email",
		"student_full_name": "student_name",
		"student_dob": "date_of_birth",
		"dob": "date_of_birth",
		"session": "course_session",
		"appointment_date_time": "appointment_datetime",
		"formname": "submitted_form_name",
		"class_session": "submitted_class_session",
		"trialclassdate": "submitted_trial_date",
		"input_radio": "referral_source",
		"Other": "referral_detail",
		"input_text_1": "referral_detail",
	}
	for source, target in aliases.items():
		if not normalized.get(target) and normalized.get(source):
			normalized[target] = normalized.get(source)

	normalized["parent_name"] = _normalize_name_value(normalized.get("parent_name"))
	normalized["student_name"] = _normalize_name_value(normalized.get("student_name"))
	normalized["contact_name"] = _normalize_name_value(normalized.get("contact_name"))
	normalized["submitted_student_name"] = _normalize_name_value(
		normalized.get("submitted_student_name") or normalized.get("student_name")
	)
	normalized["submitted_class_session"] = _normalize_scalar(normalized.get("submitted_class_session"))
	normalized["referral_source"] = _normalize_scalar(normalized.get("referral_source"))
	normalized["referral_detail"] = _normalize_scalar(normalized.get("referral_detail"))
	_normalize_appointment_aliases(normalized)
	if not normalized.get("contact_email") and normalized.get("email"):
		normalized["contact_email"] = normalized.get("email")
	if not normalized.get("contact_phone") and normalized.get("phone"):
		normalized["contact_phone"] = normalized.get("phone")
	if not normalized.get("email") and normalized.get("contact_email"):
		normalized["email"] = normalized.get("contact_email")
	if not normalized.get("phone") and normalized.get("contact_phone"):
		normalized["phone"] = normalized.get("contact_phone")
	if normalized.get("date_of_birth"):
		normalized["date_of_birth"] = _parse_date(normalized.get("date_of_birth"), "Student DOB")
	if normalized.get("submitted_student_dob"):
		normalized["submitted_student_dob"] = _parse_date(normalized.get("submitted_student_dob"), "Student DOB")
	elif normalized.get("date_of_birth"):
		normalized["submitted_student_dob"] = normalized.get("date_of_birth")
	if normalized.get("submitted_trial_date"):
		normalized["submitted_trial_date"] = _parse_date(normalized.get("submitted_trial_date"), "Trial date")
		if not normalized.get("appointment_date"):
			normalized["appointment_date"] = normalized.get("submitted_trial_date")
	if normalized.get("submitted_form_name") and not normalized.get("inquiry_type"):
		normalized["inquiry_type"] = _infer_inquiry_type(normalized)

	if normalized.get("inquiry_type"):
		normalized["inquiry_type"] = _normalize_inquiry_type(normalized.get("inquiry_type"))
	return normalized


def _normalize_webhook_payload(payload: dict):
	normalized = _normalize_inquiry_payload(payload)
	normalized["source"] = normalized.get("source") or "Webhook"
	return normalized


def _normalize_appointment_aliases(normalized: dict):
	aliases = {
		"visit_date": "appointment_date",
		"tour_date": "appointment_date",
		"school_visit_date": "appointment_date",
		"selected_date": "appointment_date",
		"booking_date": "appointment_date",
		"visit_time": "appointment_time",
		"tour_time": "appointment_time",
		"school_visit_time": "appointment_time",
		"selected_time": "appointment_time",
		"booking_time": "appointment_time",
		"visit_datetime": "appointment_datetime",
		"tour_datetime": "appointment_datetime",
		"selected_datetime": "appointment_datetime",
		"child_name": "student_name",
		"child_dob": "date_of_birth",
		"message": "referral_detail",
		"customer_message": "referral_detail",
		"interested_course": "preferred_course",
		"requested_course": "preferred_course",
	}
	for source, target in aliases.items():
		if not normalized.get(target) and normalized.get(source):
			normalized[target] = normalized.get(source)


def _infer_inquiry_type(payload: dict):
	type_hint = " ".join(
		str(value or "")
		for value in (
			payload.get("submitted_form_name"),
			payload.get("inquiry_type"),
			payload.get("request_type"),
			payload.get("type"),
		)
	).lower()
	if any(keyword in type_hint for keyword in ("school visit", "school tour", "campus visit", "tour", "visit")):
		return "School Visit"
	if payload.get("appointment_date") and payload.get("appointment_time") and not payload.get("submitted_class_session"):
		return "School Visit"
	return "Trial Lesson"


def _normalize_inquiry_type(value):
	value = (value or "").strip().lower().replace("_", " ").replace("-", " ")
	if value in {"trial", "trial lesson", "trial class"}:
		return "Trial Lesson"
	if value in {"visit", "school visit", "campus visit", "tour", "school tour", "open day"}:
		return "School Visit"
	return value.title()


def _validate_webhook_token(payload: dict):
	expected = frappe.conf.get("qas_inquiry_webhook_secret") or frappe.conf.get("inquiry_webhook_secret")
	if not expected:
		frappe.throw(_("Inquiry webhook secret is not configured."), frappe.PermissionError)

	request = getattr(frappe.local, "request", None)
	header_token = None
	if request:
		header_token = request.headers.get("X-QAS-Webhook-Token") or request.headers.get("X-Inquiry-Webhook-Token")
	token = header_token or payload.get("webhook_token") or payload.get("token")
	if token != expected:
		frappe.throw(_("Invalid inquiry webhook token."), frappe.PermissionError)


def _resolve_parent(payload: dict, inquiry_type: str | None = None):
	parent = payload.get("parent")
	if parent:
		_validate_exists("Parent", parent, _("Parent was not found."))
		_update_parent_contact_if_blank(
			parent,
			payload.get("parent_name") or payload.get("contact_name"),
			payload.get("contact_phone") or payload.get("phone"),
		)
		return parent

	parent_name = payload.get("parent_name") or payload.get("contact_name")
	phone = payload.get("contact_phone") or payload.get("phone")
	linked_user = payload.get("linked_user")
	email = (payload.get("contact_email") or payload.get("email") or "").strip().lower()
	if inquiry_type == "Trial Lesson" and payload.get("submitted_form_name") and not email:
		frappe.throw(_("Parent email is required for trial form submissions."))
	if linked_user:
		parent = frappe.db.get_value("Parent", {"linked_user": linked_user}, "name")
		if parent:
			_update_parent_contact_if_blank(parent, parent_name, phone)
			return parent

	if inquiry_type == "School Visit" and not payload.get("create_parent"):
		if email:
			user = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
			if user:
				parent = frappe.db.get_value("Parent", {"linked_user": user}, "name")
				if parent:
					_update_parent_contact_if_blank(parent, parent_name, phone)
					return parent
		return None

	user = _get_or_create_user_for_parent(email, parent_name) if email else None
	if user:
		parent = frappe.db.get_value("Parent", {"linked_user": user}, "name")
		if parent:
			_update_parent_contact_if_blank(parent, parent_name, phone)
			return parent

		parent_doc = frappe.new_doc("Parent")
		parent_doc.parent_name = parent_name or email
		parent_doc.linked_user = user
		parent_doc.mobile_number = _normalize_parent_phone(phone)
		parent_doc.flags.ignore_permissions = True
		parent_doc.insert()
		return parent_doc.name

	if payload.get("create_parent") and parent_name and linked_user:
		parent_doc = frappe.new_doc("Parent")
		parent_doc.parent_name = parent_name
		parent_doc.linked_user = linked_user
		parent_doc.mobile_number = _normalize_parent_phone(phone)
		parent_doc.flags.ignore_permissions = True
		parent_doc.insert()
		return parent_doc.name

	return None


def _resolve_student(payload: dict, parent: str | None, inquiry_type: str | None = None):
	student = payload.get("student")
	if student:
		_validate_exists("Student", student, _("Student was not found."))
		return student

	student_name = payload.get("student_name") or payload.get("submitted_student_name")
	if inquiry_type == "School Visit" and not student_name:
		return None
	student_name = student_name or "Student"
	date_of_birth = payload.get("date_of_birth") or payload.get("submitted_student_dob")
	if parent and date_of_birth:
		student = frappe.db.get_value("Student", {"guardian": parent, "date_of_birth": date_of_birth}, "name")
		if student:
			return student

	if parent and student_name:
		student_doc = frappe.new_doc("Student")
		student_doc.student_name = student_name
		student_doc.guardian = parent
		student_doc.status = payload.get("student_status") or "Inactive"
		if date_of_birth:
			student_doc.date_of_birth = date_of_birth
		else:
			student_doc.name = _make_no_dob_student_docname(student_name)
			student_doc.flags.name_set = True
		student_doc.flags.ignore_permissions = True
		student_doc.insert()
		return student_doc.name

	return None


def _get_or_create_user_for_parent(email: str | None, parent_name: str | None):
	if not email:
		return None

	user = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	if user:
		return user

	user_doc = frappe.new_doc("User")
	user_doc.email = email
	user_doc.first_name = parent_name or email
	user_doc.enabled = 1
	user_doc.user_type = "Website User"
	user_doc.send_welcome_email = 0
	user_doc.flags.ignore_permissions = True
	user_doc.insert()
	return user_doc.name


def _update_parent_contact_if_blank(parent: str, parent_name: str | None, phone: str | None):
	updates = {}
	current = frappe.db.get_value("Parent", parent, ["parent_name", "mobile_number"], as_dict=True)
	if not current:
		return
	if parent_name and not current.get("parent_name"):
		updates["parent_name"] = parent_name
	parent_phone = _normalize_parent_phone(phone)
	if parent_phone and not current.get("mobile_number"):
		updates["mobile_number"] = parent_phone
	if updates:
		frappe.db.set_value("Parent", parent, updates, update_modified=False)


def _normalize_parent_phone(phone: str | None):
	phone = (phone or "").strip()
	if not phone:
		return None
	if phone.startswith("+"):
		return phone
	digits = re.sub(r"\D", "", phone)
	if len(digits) == 10 and digits.startswith("0"):
		return "+61" + digits[1:]
	return phone


def _make_no_dob_student_docname(student_name: str):
	base = (student_name or "Student").strip()
	for _ in range(5):
		name = f"{base}-no-dob-{frappe.generate_hash(length=8)}"
		if not frappe.db.exists("Student", name):
			return name
	return f"{base}-no-dob-{frappe.generate_hash(length=12)}"


def _resolve_trial_session_context(payload: dict, inquiry_type: str):
	if inquiry_type != "Trial Lesson":
		return None, None
	if payload.get("course_session"):
		return _get_session_context(payload.get("course_session")), None
	if not (payload.get("submitted_form_name") or payload.get("submitted_class_session")):
		return None, None

	mapping = _map_trial_form_session(payload)
	if mapping.get("campus") and not payload.get("campus"):
		payload["campus"] = mapping.get("campus")
	if mapping.get("course") and not payload.get("preferred_course"):
		payload["course"] = mapping.get("course")
	if mapping.get("appointment_date") and not payload.get("appointment_date"):
		payload["appointment_date"] = mapping.get("appointment_date")
	if mapping.get("appointment_time") and not payload.get("appointment_time"):
		payload["appointment_time"] = mapping.get("appointment_time")
	if mapping.get("course_session"):
		try:
			return _get_session_context(mapping.get("course_session")), None
		except Exception as exc:
			return None, _("Matched Course Session cannot be booked: {0}").format(str(exc))
	return None, mapping.get("reason") or _("Course Session could not be matched from the submitted trial form.")


def _resolve_school_visit_context(payload: dict):
	review_reasons = []
	if not (payload.get("contact_name") or payload.get("parent_name")):
		review_reasons.append(_("Contact name is required for a school tour."))
	if not (payload.get("contact_phone") or payload.get("phone") or payload.get("contact_email") or payload.get("email")):
		review_reasons.append(_("Phone or email is required for a school tour."))

	campus = _resolve_campus(payload.get("campus"))
	if not campus:
		if payload.get("campus"):
			review_reasons.append(_("Campus could not be matched from the submitted school tour form."))
		else:
			review_reasons.append(_("Campus is required for a school tour."))

	appointment_date, appointment_time, datetime_reason = _parse_school_visit_appointment(payload)
	if datetime_reason:
		review_reasons.append(datetime_reason)

	context = {}
	if campus:
		context["campus"] = campus
	if appointment_date:
		context["appointment_date"] = appointment_date
	if appointment_time:
		context["appointment_time"] = appointment_time
	if appointment_date and appointment_time:
		context["appointment_start"] = datetime.combine(getdate(appointment_date), get_time(appointment_time))
		context["appointment_end"] = context["appointment_start"] + timedelta(minutes=15)

	return context or None, "; ".join(str(reason) for reason in review_reasons if reason) or None


def _map_trial_form_session(payload: dict):
	form_name = payload.get("submitted_form_name")
	class_session = payload.get("submitted_class_session")
	trial_date = payload.get("submitted_trial_date") or payload.get("appointment_date")
	campus, course_candidate = _derive_campus_and_course(form_name)
	parsed_session = _parse_class_session(class_session)

	result = {
		"campus": campus,
		"appointment_date": trial_date,
		"appointment_time": parsed_session.get("start_time") if parsed_session else None,
	}
	if not campus:
		result["reason"] = _("Campus could not be derived from submitted form name.")
		return result
	if not parsed_session:
		result["reason"] = _("Class session time could not be parsed from submitted form.")
		return result
	if not trial_date:
		result["reason"] = _("Trial date was not submitted.")
		return result

	course = _resolve_course(course_candidate)
	if course:
		result["course"] = course

	timeslot_filters = {
		"campus": campus,
		"day_of_week": parsed_session["day_of_week"],
		"start_time": parsed_session["start_time"],
	}
	if course:
		timeslot_filters["course"] = course
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters=timeslot_filters,
		fields=["name", "course", "campus", "start_time", "end_time"],
		order_by="modified desc",
	)
	if not timeslots and course:
		timeslot_filters.pop("course")
		timeslots = frappe.get_all(
			"Weekly Timeslot",
			filters=timeslot_filters,
			fields=["name", "course", "campus", "start_time", "end_time"],
			order_by="modified desc",
		)
	if not timeslots:
		result["reason"] = _("No Weekly Timeslot matched the submitted campus, weekday, and time.")
		return result
	if len(timeslots) > 1:
		result["reason"] = _("Multiple Weekly Timeslots matched the submitted campus, weekday, and time.")
		return result

	result["course"] = timeslots[0].course
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": timeslots[0].name, "session_date": trial_date},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="modified desc",
	)
	if not sessions:
		result["reason"] = _("No Course Session exists for the matched Weekly Timeslot and trial date.")
		return result
	if len(sessions) > 1:
		result["reason"] = _("Multiple Course Sessions matched the submitted trial request.")
		return result

	result["course_session"] = sessions[0].name
	return result


def _derive_campus_and_course(form_name: str | None):
	form_name = (form_name or "").strip()
	if not form_name:
		return None, None

	campuses = frappe.get_all("Campus", fields=["name"])
	matches = [
		row.name for row in campuses if _normalize_compare(row.name) and _normalize_compare(row.name) in _normalize_compare(form_name)
	]
	campus = sorted(matches, key=len, reverse=True)[0] if matches else None
	course_candidate = form_name
	if campus:
		course_candidate = re.sub(re.escape(campus), "", course_candidate, flags=re.IGNORECASE).strip(" -")
	return campus, course_candidate or None


def _resolve_course(course_candidate: str | None):
	candidate = (course_candidate or "").strip()
	if not candidate:
		return None
	if frappe.db.exists("Course", candidate):
		return candidate
	courses = frappe.get_all("Course", fields=["name"])
	normalized_candidate = _normalize_compare(candidate)
	matches = [
		row.name
		for row in courses
		if normalized_candidate
		and (
			_normalize_compare(row.name) == normalized_candidate
			or normalized_candidate in _normalize_compare(row.name)
			or _normalize_compare(row.name) in normalized_candidate
		)
	]
	return matches[0] if len(matches) == 1 else None


def _resolve_campus(campus_candidate: str | None):
	candidate = (campus_candidate or "").strip()
	if not candidate:
		return None
	if frappe.db.exists("Campus", candidate):
		return candidate
	campuses = frappe.get_all("Campus", fields=["name"])
	normalized_candidate = _normalize_compare(candidate)
	matches = [
		row.name
		for row in campuses
		if normalized_candidate
		and (
			_normalize_compare(row.name) == normalized_candidate
			or normalized_candidate in _normalize_compare(row.name)
			or _normalize_compare(row.name) in normalized_candidate
		)
	]
	return matches[0] if len(matches) == 1 else None


def _parse_class_session(class_session: str | None):
	value = (class_session or "").strip()
	match = re.search(r"([A-Za-z]+)\s+(\d{1,2}:\d{2})(?:\s*[-–]\s*(\d{1,2}:\d{2}))?", value)
	if not match:
		return None

	day = DAY_ABBREVIATIONS.get(match.group(1).lower())
	if not day:
		return None
	return {
		"day_of_week": day,
		"start_time": _normalize_time_string(match.group(2)),
		"end_time": _normalize_time_string(match.group(3)) if match.group(3) else None,
	}


def _normalize_name_value(value):
	if isinstance(value, dict):
		parts = [
			value.get("first_name"),
			value.get("middle_name"),
			value.get("last_name"),
		]
		return " ".join(str(part).strip() for part in parts if part).strip()
	return _normalize_scalar(value)


def _normalize_scalar(value):
	if isinstance(value, list):
		return ", ".join(str(item).strip() for item in value if str(item).strip())
	if isinstance(value, dict):
		return " ".join(str(item).strip() for item in value.values() if str(item).strip())
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def _parse_date(value, label):
	if not value:
		return None
	if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
		return getdate(value)
	for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y"):
		try:
			return datetime.strptime(str(value).strip(), fmt).date()
		except ValueError:
			pass
	try:
		return getdate(value)
	except Exception:
		frappe.throw(_("{0} is invalid.").format(label))


def _try_parse_date(value):
	if not value:
		return None
	try:
		return _parse_date(value, "Date")
	except Exception:
		return None


def _try_parse_time(value):
	if not value:
		return None
	try:
		return _normalize_time_string(value)
	except Exception:
		return None


def _parse_school_visit_appointment(payload: dict):
	appointment_date = _try_parse_date(payload.get("appointment_date"))
	appointment_time = _try_parse_time(payload.get("appointment_time"))
	appointment_datetime = payload.get("appointment_datetime")

	if appointment_datetime and (not appointment_date or not appointment_time):
		try:
			value = datetime.fromisoformat(str(appointment_datetime).strip().replace("Z", "+00:00"))
			appointment_date = appointment_date or value.date()
			appointment_time = appointment_time or value.time().replace(tzinfo=None).strftime("%H:%M:%S")
		except ValueError:
			return appointment_date, appointment_time, _("School tour appointment datetime is invalid.")

	missing = []
	if payload.get("appointment_date") and not appointment_date:
		missing.append(_("appointment date is invalid"))
	elif not appointment_date:
		missing.append(_("appointment date is required"))
	if payload.get("appointment_time") and not appointment_time:
		missing.append(_("appointment time is invalid"))
	elif not appointment_time:
		missing.append(_("appointment time is required"))

	return appointment_date, appointment_time, "; ".join(str(item) for item in missing if item) or None


def _normalize_time_string(value):
	if not value:
		return None
	return get_time(str(value)).strftime("%H:%M:%S")


def _normalize_compare(value):
	return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _get_initial_inquiry_status(appointment_data, review_reason: str | None):
	if review_reason:
		return NEEDS_REVIEW_STATUS
	if appointment_data:
		return "Booked"
	return "New"


def sync_inquiry_course_session(inquiry_doc):
	if inquiry_doc.inquiry_type != "Trial Lesson":
		return

	old_doc = None if inquiry_doc.is_new() else inquiry_doc.get_doc_before_save()
	old_course_session = old_doc.course_session if old_doc else None

	if inquiry_doc.status == "Cancelled":
		if not inquiry_doc.is_new():
			cancel_trial_inquiry_attendance_entries(inquiry_doc.name)
		return

	if old_course_session and old_course_session != inquiry_doc.course_session:
		remove_trial_inquiry_attendance_entries(inquiry_doc.name)

	if not inquiry_doc.course_session:
		return
	if not inquiry_doc.student:
		frappe.throw(_("Student is required before assigning a trial lesson course session."))

	session_context = _get_session_context(inquiry_doc.course_session)
	_apply_session_to_inquiry(inquiry_doc, session_context)
	inquiry_doc.review_reason = None
	if not inquiry_doc.status or inquiry_doc.status in {"New", NEEDS_REVIEW_STATUS, "Follow-up"}:
		inquiry_doc.status = "Booked"
	if not inquiry_doc.confirmation_status or inquiry_doc.confirmation_status == "Not Required":
		inquiry_doc.confirmation_status = "Pending"
	if not inquiry_doc.is_new():
		ensure_inquiry_attendance_entry(inquiry_doc)


def ensure_inquiry_attendance_entry(inquiry_doc):
	return ensure_trial_inquiry_attendance_entry(inquiry_doc)


def _apply_session_to_inquiry(inquiry_doc, session_context):
	inquiry_doc.course_session = session_context["session"].get("name")
	inquiry_doc.campus = session_context.get("campus")
	inquiry_doc.preferred_course = session_context["timeslot"].get("course")
	inquiry_doc.current_appointment_date = session_context["session"].get("session_date")
	inquiry_doc.current_appointment_time = session_context["timeslot"].get("start_time")


def _get_requested_trial_datetime(payload: dict):
	appointment_time = payload.get("appointment_time")
	if not appointment_time and payload.get("submitted_class_session"):
		parsed = _parse_class_session(payload.get("submitted_class_session"))
		appointment_time = parsed.get("start_time") if parsed else None
	return payload.get("appointment_date") or payload.get("submitted_trial_date"), appointment_time


def _parse_appointment_datetime(payload: dict):
	appointment_date = payload.get("appointment_date")
	appointment_time = payload.get("appointment_time")
	appointment_datetime = payload.get("appointment_datetime")
	if appointment_datetime and not appointment_date:
		try:
			value = datetime.fromisoformat(str(appointment_datetime).replace("Z", "+00:00"))
			appointment_date = value.date()
			appointment_time = value.time().replace(tzinfo=None)
		except ValueError:
			frappe.throw(_("Appointment datetime is invalid."))
	return appointment_date, appointment_time


def _get_session_context(course_session: str | None):
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
	if not session.get("weekly_timeslot"):
		frappe.throw(_("Course session is missing a weekly timeslot."))

	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		session.get("weekly_timeslot"),
		["name", "course", "class_language", "campus", "classroom", "teacher", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))
	_get_session_start(session, timeslot)
	return {"session": session, "timeslot": timeslot, "campus": timeslot.get("campus")}


def _get_session_start(session, timeslot):
	if not session.get("session_date") or not timeslot.get("start_time"):
		frappe.throw(_("Course session is missing date or start time."))
	return datetime.combine(getdate(session.get("session_date")), get_time(timeslot.get("start_time")))


def _send_needs_review_alert(inquiry_doc, reason: str):
	recipients = _get_admin_alert_recipients()
	if not recipients:
		return

	try:
		sendmail_or_skip(
			action="inquiry_needs_review_alert",
			recipients=recipients,
			subject=_("Inquiry Needs Review: {0}").format(inquiry_doc.name),
			message=_build_needs_review_email(inquiry_doc, reason),
			delayed=True,
		)
	except Exception as exc:
		frappe.log_error(
			title="Inquiry Needs Review Alert Failed",
			message=json.dumps({"inquiry": inquiry_doc.name, "error": str(exc), "recipients": recipients}, default=str),
		)


def _get_admin_alert_recipients():
	configured = frappe.conf.get("qas_inquiry_admin_emails") or frappe.conf.get("qas_inquiry_admin_email")
	recipients = []
	if isinstance(configured, str):
		recipients.extend([email.strip() for email in configured.split(",") if email.strip()])
	elif isinstance(configured, (list, tuple, set)):
		recipients.extend([str(email).strip() for email in configured if str(email).strip()])
	if recipients:
		return sorted(set(recipients))

	role_users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", sorted(ADMIN_ROLES)], "parenttype": "User"},
		fields=["parent"],
		distinct=True,
	)
	user_ids = [row.parent for row in role_users if row.parent]
	if not user_ids:
		return []
	users = frappe.get_all(
		"User",
		filters={"name": ["in", user_ids], "enabled": 1},
		fields=["email"],
	)
	return sorted({row.email for row in users if row.email and row.email != "Administrator"})


def _build_needs_review_email(inquiry_doc, reason: str):
	lines = [
		_("An inquiry needs manual review."),
		"",
		_("Inquiry: {0}").format(inquiry_doc.name),
		_("Type: {0}").format(inquiry_doc.inquiry_type or "-"),
		_("Reason: {0}").format(reason),
		_("Campus: {0}").format(inquiry_doc.campus or "-"),
		_("Appointment: {0} {1}").format(
			inquiry_doc.current_appointment_date or "-",
			inquiry_doc.current_appointment_time or "-",
		),
		_("Submitted form: {0}").format(inquiry_doc.submitted_form_name or "-"),
		_("Submitted session: {0}").format(inquiry_doc.submitted_class_session or "-"),
		_("Submitted trial date: {0}").format(inquiry_doc.submitted_trial_date or "-"),
		_("Parent: {0}").format(inquiry_doc.parent or "-"),
		_("Student: {0}").format(inquiry_doc.student or "-"),
		_("Contact: {0} / {1} / {2}").format(
			inquiry_doc.contact_name or "-",
			inquiry_doc.contact_email or "-",
			inquiry_doc.contact_phone or "-",
		),
	]
	return "<br>".join(frappe.utils.escape_html(line) for line in lines)


def _build_inquiry_payload(doc):
	return {
		"id": doc.name,
		"inquiry_id": doc.name,
		"inquiry_type": doc.inquiry_type,
		"source": doc.source,
		"status": doc.status,
		"campus": doc.campus,
		"parent": doc.parent,
		"student": doc.student,
		"contact_name": doc.contact_name,
		"contact_phone": doc.contact_phone,
		"contact_email": doc.contact_email,
		"submitted_form_name": doc.submitted_form_name,
		"submitted_student_name": doc.submitted_student_name,
		"submitted_student_dob": _as_string(doc.submitted_student_dob),
		"submitted_class_session": doc.submitted_class_session,
		"submitted_trial_date": _as_string(doc.submitted_trial_date),
		"referral_source": doc.referral_source,
		"referral_detail": doc.referral_detail,
		"preferred_course": doc.preferred_course,
		"course_session": doc.course_session,
		"current_appointment_date": _as_string(doc.current_appointment_date),
		"current_appointment_time": _as_string(doc.current_appointment_time),
		"review_reason": doc.review_reason,
		"confirmation_status": doc.confirmation_status,
		"reminder_status": doc.reminder_status,
		"trial_invoice": doc.trial_invoice,
		"converted_enrollment": doc.converted_enrollment,
		"converted_invoice": doc.get("converted_invoice"),
		"inactive_reason": doc.inactive_reason,
	}


def _get_note_payloads(inquiry):
	fields = ["name", "student", "note", "author", "edited_at", "creation"]
	meta = frappe.get_meta("Inquiry Note")
	for fieldname in ("note_type", "source_doctype", "source_document"):
		if meta.has_field(fieldname):
			fields.append(fieldname)
	return [
		{
			"id": row.name,
			"student": row.student,
			"note": row.note,
			"author": row.author,
			"edited_at": _as_string(row.edited_at),
			"creation": _as_string(row.creation),
			"note_type": row.get("note_type") or "Manual",
			"source_doctype": row.get("source_doctype"),
			"source_document": row.get("source_document"),
		}
		for row in frappe.get_all(
			"Inquiry Note",
			filters={"inquiry": inquiry},
			fields=fields,
			order_by="creation desc",
		)
	]


def _validate_exists(doctype: str, name: str, message):
	if not frappe.db.exists(doctype, name):
		frappe.throw(message)


def _get_payload(payload=None):
	if payload is not None:
		if isinstance(payload, str):
			try:
				return json.loads(payload)
			except json.JSONDecodeError:
				frappe.throw(_("Payload must be valid JSON."))
		return payload if isinstance(payload, dict) else {}

	request = getattr(frappe.local, "request", None)
	if request:
		json_payload = request.get_json(silent=True)
		if isinstance(json_payload, dict):
			return json_payload
	if frappe.form_dict:
		return dict(frappe.form_dict)
	return {}


def _as_string(value):
	if value is None:
		return None
	return str(value)
