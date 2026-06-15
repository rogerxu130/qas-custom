from __future__ import annotations

import json
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import get_time, getdate, now_datetime


INQUIRY_TYPES = {"Trial Lesson", "School Visit"}
ADMIN_ROLES = {"System Manager", "School Admin"}
TRIAL_ENROLLMENT_TYPE = "Trial"
DEFAULT_ATTENDANCE_STATUS = "To be started"


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


def reschedule_inquiry_data(inquiry=None, payload=None):
	_require_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	return reschedule_inquiry_core(inquiry, payload, actor=frappe.session.user)


def mark_inquiry_completed_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "Completed", actor=frappe.session.user)


def mark_inquiry_no_show_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "No-show", actor=frappe.session.user)


def mark_inquiry_follow_up_data(inquiry=None):
	_require_admin()
	return mark_inquiry_status_core(inquiry, "Follow-up", actor=frappe.session.user)


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

	parent = _resolve_parent(payload)
	student = _resolve_student(payload, parent)
	customer = _resolve_customer(payload, parent)
	session_context = _get_session_context(payload.get("course_session")) if payload.get("course_session") else None
	appointment_data = _build_appointment_data(payload, inquiry_type, session_context)

	if inquiry_type == "Trial Lesson" and appointment_data and not student:
		frappe.throw(_("A linked student is required before booking a trial lesson into a course session."))

	inquiry_doc = frappe.new_doc("Inquiry")
	inquiry_doc.inquiry_type = inquiry_type
	inquiry_doc.source = source or payload.get("source") or "Manual"
	inquiry_doc.status = "Booked" if appointment_data else "New"
	inquiry_doc.campus = appointment_data.get("campus") if appointment_data else payload.get("campus")
	inquiry_doc.parent = parent
	inquiry_doc.student = student
	inquiry_doc.customer = customer
	inquiry_doc.contact_name = payload.get("contact_name") or payload.get("parent_name")
	inquiry_doc.contact_phone = payload.get("contact_phone") or payload.get("phone")
	inquiry_doc.contact_email = payload.get("contact_email") or payload.get("email")
	inquiry_doc.preferred_course = payload.get("preferred_course") or payload.get("course")
	inquiry_doc.confirmation_status = "Pending" if appointment_data else "Not Required"
	inquiry_doc.reminder_status = "Not Required"
	inquiry_doc.flags.ignore_permissions = True
	inquiry_doc.insert()

	appointment_doc = None
	if appointment_data:
		appointment_doc = _create_appointment(inquiry_doc.name, appointment_data, previous_appointment=None)
		if inquiry_type == "Trial Lesson":
			attendance_row_id = _add_trial_attendance_row(
				course_session=appointment_doc.course_session,
				student=student,
				inquiry=inquiry_doc.name,
			)
			appointment_doc.attendance_row_id = attendance_row_id
			appointment_doc.save(ignore_permissions=True)

		_apply_current_appointment(inquiry_doc, appointment_doc)
		inquiry_doc.save(ignore_permissions=True)

	_write_history(
		inquiry_doc.name,
		"inquiry_created",
		actor=actor,
		after=_build_history_snapshot(inquiry_doc),
		message="Inquiry created.",
	)
	if appointment_doc:
		_write_history(
			inquiry_doc.name,
			"appointment_booked",
			actor=actor,
			after=_build_history_snapshot(appointment_doc),
			message="Appointment booked.",
		)

	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def reschedule_inquiry_core(inquiry: str | None, payload: dict, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))

	payload = _normalize_inquiry_payload(payload)
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status in {"Completed", "Converted", "Inactive"}:
		frappe.throw(_("This inquiry cannot be rescheduled from its current status."))

	current_appointment = _get_current_appointment_doc(inquiry_doc)
	if not current_appointment:
		frappe.throw(_("This inquiry does not have a current appointment to reschedule."))

	before_snapshot = {
		"inquiry": _build_history_snapshot(inquiry_doc),
		"appointment": _build_history_snapshot(current_appointment),
	}
	old_started = _appointment_has_started(current_appointment)
	if old_started:
		current_appointment.status = "No-show"
	else:
		current_appointment.status = "Rescheduled"
	current_appointment.is_current = 0
	current_appointment.save(ignore_permissions=True)

	if inquiry_doc.inquiry_type == "Trial Lesson":
		session_context = _get_session_context(payload.get("course_session"))
		if old_started:
			_write_history(
				inquiry_doc.name,
				"appointment_no_show_before_rebook",
				actor=actor,
				before=before_snapshot,
				message="Previous trial appointment had already started before rebooking.",
			)
		else:
			_remove_trial_attendance_row(current_appointment)
	else:
		session_context = None

	appointment_data = _build_appointment_data(payload, inquiry_doc.inquiry_type, session_context)
	new_appointment = _create_appointment(
		inquiry_doc.name,
		appointment_data,
		previous_appointment=current_appointment.name,
	)
	if inquiry_doc.inquiry_type == "Trial Lesson":
		attendance_row_id = _add_trial_attendance_row(
			course_session=new_appointment.course_session,
			student=inquiry_doc.student,
			inquiry=inquiry_doc.name,
		)
		new_appointment.attendance_row_id = attendance_row_id
		new_appointment.save(ignore_permissions=True)

	inquiry_doc.status = "Rescheduled"
	inquiry_doc.confirmation_status = "Pending"
	_apply_current_appointment(inquiry_doc, new_appointment)
	inquiry_doc.save(ignore_permissions=True)

	_write_history(
		inquiry_doc.name,
		"appointment_rescheduled",
		actor=actor,
		before=before_snapshot,
		after={
			"inquiry": _build_history_snapshot(inquiry_doc),
			"appointment": _build_history_snapshot(new_appointment),
		},
		message="Inquiry appointment rescheduled.",
	)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def mark_inquiry_status_core(inquiry: str | None, status: str, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	if status not in {"Completed", "No-show", "Follow-up"}:
		frappe.throw(_("Unsupported inquiry status."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	before = _build_history_snapshot(inquiry_doc)
	inquiry_doc.status = status
	inquiry_doc.save(ignore_permissions=True)

	current_appointment = _get_current_appointment_doc(inquiry_doc)
	if current_appointment and status in {"Completed", "No-show"}:
		current_appointment.status = status
		current_appointment.save(ignore_permissions=True)

	_write_history(
		inquiry_doc.name,
		"inquiry_status_changed",
		actor=actor,
		before=before,
		after=_build_history_snapshot(inquiry_doc),
		message=f"Inquiry marked {status}.",
	)
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
	note_doc.flags.ignore_permissions = True
	note_doc.insert()

	_write_history(
		inquiry_doc.name,
		"inquiry_note_added",
		actor=actor,
		after={"note": note_doc.name},
		message="Inquiry note added.",
	)
	frappe.db.commit()
	return build_inquiry_detail(inquiry_doc.name)


def build_inquiry_detail(inquiry: str):
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	return {
		"inquiry": _build_inquiry_payload(inquiry_doc),
		"appointments": _get_appointment_payloads(inquiry_doc.name),
		"notes": _get_note_payloads(inquiry_doc.name),
		"history": _get_history_payloads(inquiry_doc.name),
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
		"session": "course_session",
		"appointment_date_time": "appointment_datetime",
	}
	for source, target in aliases.items():
		if not normalized.get(target) and normalized.get(source):
			normalized[target] = normalized.get(source)

	if normalized.get("inquiry_type"):
		normalized["inquiry_type"] = _normalize_inquiry_type(normalized.get("inquiry_type"))
	return normalized


def _normalize_webhook_payload(payload: dict):
	normalized = _normalize_inquiry_payload(payload)
	normalized["source"] = normalized.get("source") or "Webhook"
	return normalized


def _normalize_inquiry_type(value):
	value = (value or "").strip().lower().replace("_", " ").replace("-", " ")
	if value in {"trial", "trial lesson", "trial class"}:
		return "Trial Lesson"
	if value in {"visit", "school visit", "campus visit", "tour"}:
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


def _resolve_parent(payload: dict):
	parent = payload.get("parent")
	if parent:
		_validate_exists("Parent", parent, _("Parent was not found."))
		return parent

	parent_name = payload.get("parent_name") or payload.get("contact_name")
	phone = payload.get("contact_phone") or payload.get("phone")
	linked_user = payload.get("linked_user")
	if linked_user:
		parent = frappe.db.get_value("Parent", {"linked_user": linked_user}, "name")
		if parent:
			return parent
	if parent_name:
		parent = frappe.db.get_value("Parent", {"parent_name": parent_name}, "name")
		if parent:
			return parent
	if phone:
		parent = frappe.db.get_value("Parent", {"mobile_number": phone}, "name")
		if parent:
			return parent

	if payload.get("create_parent") and parent_name and linked_user:
		parent_doc = frappe.new_doc("Parent")
		parent_doc.parent_name = parent_name
		parent_doc.linked_user = linked_user
		parent_doc.mobile_number = phone
		parent_doc.flags.ignore_permissions = True
		parent_doc.insert()
		return parent_doc.name

	return None


def _resolve_student(payload: dict, parent: str | None):
	student = payload.get("student")
	if student:
		_validate_exists("Student", student, _("Student was not found."))
		return student

	student_name = payload.get("student_name")
	if student_name and parent:
		student = frappe.db.get_value("Student", {"student_name": student_name, "guardian": parent}, "name")
		if student:
			return student

	if payload.get("create_student") and student_name and parent and payload.get("date_of_birth"):
		student_doc = frappe.new_doc("Student")
		student_doc.student_name = student_name
		student_doc.guardian = parent
		student_doc.status = payload.get("student_status") or "Inactive"
		student_doc.date_of_birth = payload.get("date_of_birth")
		student_doc.flags.ignore_permissions = True
		student_doc.insert()
		return student_doc.name

	return None


def _resolve_customer(payload: dict, parent: str | None):
	customer = payload.get("customer")
	if customer:
		_validate_exists("Customer", customer, _("Customer was not found."))
		return customer
	if parent:
		return frappe.db.get_value("Parent", parent, "customer")
	return None


def _build_appointment_data(payload: dict, inquiry_type: str, session_context=None):
	has_appointment = bool(
		payload.get("course_session")
		or payload.get("appointment_date")
		or payload.get("appointment_datetime")
	)
	if not has_appointment:
		return None

	if inquiry_type == "Trial Lesson":
		if not session_context:
			frappe.throw(_("Course session is required for a trial lesson appointment."))
		return {
			"appointment_type": inquiry_type,
			"campus": session_context["campus"],
			"course_session": session_context["session"]["name"],
			"appointment_date": session_context["session"]["session_date"],
			"appointment_time": session_context["timeslot"]["start_time"],
			"reason": payload.get("reason"),
		}

	appointment_date, appointment_time = _parse_appointment_datetime(payload)
	if not appointment_date:
		frappe.throw(_("Appointment date is required for a school visit."))
	if not payload.get("campus"):
		frappe.throw(_("Campus is required for a school visit."))
	return {
		"appointment_type": inquiry_type,
		"campus": payload.get("campus"),
		"course_session": None,
		"appointment_date": appointment_date,
		"appointment_time": appointment_time,
		"reason": payload.get("reason"),
	}


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


def _create_appointment(inquiry: str, appointment_data: dict, previous_appointment=None):
	appointment_doc = frappe.new_doc("Inquiry Appointment")
	appointment_doc.inquiry = inquiry
	appointment_doc.appointment_type = appointment_data["appointment_type"]
	appointment_doc.status = "Booked"
	appointment_doc.is_current = 1
	appointment_doc.campus = appointment_data.get("campus")
	appointment_doc.course_session = appointment_data.get("course_session")
	appointment_doc.appointment_date = appointment_data.get("appointment_date")
	appointment_doc.appointment_time = appointment_data.get("appointment_time")
	appointment_doc.previous_appointment = previous_appointment
	appointment_doc.reason = appointment_data.get("reason")
	appointment_doc.flags.ignore_permissions = True
	appointment_doc.insert()
	return appointment_doc


def _apply_current_appointment(inquiry_doc, appointment_doc):
	inquiry_doc.current_appointment = appointment_doc.name
	inquiry_doc.current_course_session = appointment_doc.course_session
	inquiry_doc.current_appointment_date = appointment_doc.appointment_date
	inquiry_doc.current_appointment_time = appointment_doc.appointment_time
	inquiry_doc.campus = appointment_doc.campus


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
		["name", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot was not found."))
	if _get_session_start(session, timeslot) <= now_datetime():
		frappe.throw(_("Course session has already started."))
	return {"session": session, "timeslot": timeslot, "campus": timeslot.get("campus")}


def _get_session_start(session, timeslot):
	if not session.get("session_date") or not timeslot.get("start_time"):
		frappe.throw(_("Course session is missing date or start time."))
	return datetime.combine(getdate(session.get("session_date")), get_time(timeslot.get("start_time")))


def _appointment_has_started(appointment_doc):
	if not appointment_doc.appointment_date:
		return False
	appointment_time = appointment_doc.appointment_time or "00:00:00"
	return datetime.combine(getdate(appointment_doc.appointment_date), get_time(appointment_time)) <= now_datetime()


def _add_trial_attendance_row(course_session: str, student: str, inquiry: str):
	if not student:
		frappe.throw(_("Student is required before adding trial attendance."))
	session_doc = frappe.get_doc("Course Sessions", course_session)
	for row in session_doc.get("attendance_list", []):
		if row.student == student and row.enrollment_type == TRIAL_ENROLLMENT_TYPE:
			frappe.throw(_("This student is already listed as a trial student for the selected session."))

	row = session_doc.append(
		"attendance_list",
		{
			"student": student,
			"enrollment_type": TRIAL_ENROLLMENT_TYPE,
			"status": DEFAULT_ATTENDANCE_STATUS,
			"comments": f"Added from Inquiry {inquiry}",
		},
	)
	session_doc.save(ignore_permissions=True)
	return row.name


def _remove_trial_attendance_row(appointment_doc):
	if not appointment_doc.course_session or not appointment_doc.attendance_row_id:
		return

	session_doc = frappe.get_doc("Course Sessions", appointment_doc.course_session)
	target = None
	for row in session_doc.get("attendance_list", []):
		if row.name == appointment_doc.attendance_row_id and row.enrollment_type == TRIAL_ENROLLMENT_TYPE:
			target = row
			break
	if target:
		session_doc.remove(target)
		session_doc.save(ignore_permissions=True)


def _get_current_appointment_doc(inquiry_doc):
	if not inquiry_doc.current_appointment:
		return None
	return frappe.get_doc("Inquiry Appointment", inquiry_doc.current_appointment)


def _write_history(inquiry, event_type, actor=None, before=None, after=None, message=None):
	history = frappe.new_doc("Inquiry History")
	history.inquiry = inquiry
	history.event_type = event_type
	history.actor = actor
	history.event_time = now_datetime()
	history.before_json = json.dumps(before, default=str) if before is not None else None
	history.after_json = json.dumps(after, default=str) if after is not None else None
	history.message = message
	history.flags.ignore_permissions = True
	history.insert()
	return history


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
		"customer": doc.customer,
		"contact_name": doc.contact_name,
		"contact_phone": doc.contact_phone,
		"contact_email": doc.contact_email,
		"preferred_course": doc.preferred_course,
		"current_appointment": doc.current_appointment,
		"current_course_session": doc.current_course_session,
		"current_appointment_date": _as_string(doc.current_appointment_date),
		"current_appointment_time": _as_string(doc.current_appointment_time),
		"confirmation_status": doc.confirmation_status,
		"reminder_status": doc.reminder_status,
		"trial_invoice": doc.trial_invoice,
		"converted_enrollment": doc.converted_enrollment,
		"inactive_reason": doc.inactive_reason,
	}


def _get_appointment_payloads(inquiry):
	return [
		{
			"id": row.name,
			"appointment_type": row.appointment_type,
			"status": row.status,
			"is_current": bool(row.is_current),
			"campus": row.campus,
			"course_session": row.course_session,
			"attendance_row_id": row.attendance_row_id,
			"appointment_date": _as_string(row.appointment_date),
			"appointment_time": _as_string(row.appointment_time),
			"previous_appointment": row.previous_appointment,
			"reason": row.reason,
		}
		for row in frappe.get_all(
			"Inquiry Appointment",
			filters={"inquiry": inquiry},
			fields=[
				"name",
				"appointment_type",
				"status",
				"is_current",
				"campus",
				"course_session",
				"attendance_row_id",
				"appointment_date",
				"appointment_time",
				"previous_appointment",
				"reason",
			],
			order_by="appointment_date desc, creation desc",
		)
	]


def _get_note_payloads(inquiry):
	return [
		{
			"id": row.name,
			"student": row.student,
			"note": row.note,
			"author": row.author,
			"edited_at": _as_string(row.edited_at),
			"creation": _as_string(row.creation),
		}
		for row in frappe.get_all(
			"Inquiry Note",
			filters={"inquiry": inquiry},
			fields=["name", "student", "note", "author", "edited_at", "creation"],
			order_by="creation desc",
		)
	]


def _get_history_payloads(inquiry):
	return [
		{
			"id": row.name,
			"event_type": row.event_type,
			"actor": row.actor,
			"event_time": _as_string(row.event_time),
			"message": row.message,
			"before_json": row.before_json,
			"after_json": row.after_json,
		}
		for row in frappe.get_all(
			"Inquiry History",
			filters={"inquiry": inquiry},
			fields=["name", "event_type", "actor", "event_time", "message", "before_json", "after_json"],
			order_by="event_time desc, creation desc",
		)
	]


def _build_history_snapshot(doc):
	return {field: doc.get(field) for field in doc.meta.get_valid_columns() if field not in {"modified", "modified_by"}}


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
