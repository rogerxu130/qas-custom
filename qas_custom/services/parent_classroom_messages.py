from __future__ import annotations

from collections import defaultdict
import re

import frappe
from frappe import _
from frappe.utils import add_days, cint, escape_html, getdate, now_datetime, validate_email_address

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.services.support_view import reject_support_view_write
from qas_custom.utils.environment import email_block_reason, sendmail_or_skip


MESSAGE_DOCTYPE = "Parent Classroom Message"
ATTEMPT_DOCTYPE = "Parent Classroom Message Attempt"
ATTENDANCE_DOCTYPE = "Class Attendance Entry"
MESSAGE_CATEGORIES = (
	"Behaviour concern",
	"Learning progress",
	"Participation",
	"Materials / homework",
	"Other",
)
MAX_MESSAGE_LENGTH = 2000
ADMIN_ROLES = {"School Admin", "System Manager"}


def create_teacher_parent_classroom_message_data(
	course_session=None,
	attendance_entry=None,
	student=None,
	category=None,
	message=None,
	client_request_id=None,
):
	reject_support_view_write()
	teacher = _require_teacher()
	client_request_id = _required_text(client_request_id, _("Client request ID is required."), max_length=140)
	existing = frappe.db.get_value(MESSAGE_DOCTYPE, {"client_request_id": client_request_id}, ["name", "teacher"], as_dict=True)
	if existing:
		if existing.teacher != teacher.name:
			frappe.throw(_("This message request ID is already in use."), frappe.PermissionError)
		return {"message": _message_payload(existing.name), "duplicate": True}

	context = _teacher_message_context(teacher, course_session, attendance_entry, student)
	category = _valid_category(category)
	message = _valid_message(message)
	mail_context = _school_mail_context()

	doc = frappe.new_doc(MESSAGE_DOCTYPE)
	doc.course_session = context["session"].name
	doc.attendance_entry = context["attendance"].name
	doc.student = context["student"].name
	doc.parent = context["parent"].name
	doc.teacher = teacher.name
	doc.category = category
	doc.message = message
	doc.recipient_email = context["recipient_email"]
	doc.client_request_id = client_request_id
	doc.status = "Queued"
	doc.attempt_count = 1
	doc.created_by_user = frappe.session.user
	_append_attempt(doc, 1, frappe.session.user, mail_context)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	_queue_delivery_or_mark_failed(doc.name, 1)
	return {"message": _message_payload(doc.name), "duplicate": False}


def get_teacher_parent_classroom_messages_data(course_session=None, student=None, limit=50):
	teacher = _require_teacher()
	if not course_session:
		frappe.throw(_("Course Session is required."))
	_get_owned_session(course_session, teacher.name)
	filters = {"teacher": teacher.name, "course_session": course_session}
	if student:
		filters["student"] = student
	rows = frappe.get_all(
		MESSAGE_DOCTYPE,
		filters=filters,
		fields=_message_fields(),
		order_by="creation desc",
		limit_page_length=_limit(limit, 50, 100),
	)
	return {"items": _enrich_message_rows(rows)}


def retry_teacher_parent_classroom_message_data(parent_classroom_message=None):
	reject_support_view_write()
	teacher = _require_teacher()
	if not parent_classroom_message:
		frappe.throw(_("Parent classroom message is required."))
	frappe.db.sql("select name from `tabParent Classroom Message` where name = %s for update", (parent_classroom_message,))
	doc = frappe.get_doc(MESSAGE_DOCTYPE, parent_classroom_message)
	if doc.teacher != teacher.name:
		frappe.throw(_("You do not have access to this message."), frappe.PermissionError)
	_get_owned_session(doc.course_session, teacher.name)
	if doc.status != "Failed":
		frappe.throw(_("Only a failed message can be retried."))
	if any(row.status == "Queued" for row in doc.attempts or []):
		frappe.throw(_("This message already has a queued delivery attempt."))

	mail_context = _school_mail_context()
	next_attempt = cint(doc.attempt_count) + 1
	doc.status = "Queued"
	doc.attempt_count = next_attempt
	_append_attempt(doc, next_attempt, frappe.session.user, mail_context)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	_queue_delivery_or_mark_failed(doc.name, next_attempt)
	return {"message": _message_payload(doc.name)}


def get_school_admin_parent_classroom_messages_data(
	query=None,
	from_date=None,
	to_date=None,
	teacher=None,
	student=None,
	campus=None,
	category=None,
	status=None,
	limit_start=0,
	limit=100,
):
	_require_school_admin()
	filters = []
	if from_date:
		filters.append([MESSAGE_DOCTYPE, "creation", ">=", getdate(from_date)])
	if to_date:
		filters.append([MESSAGE_DOCTYPE, "creation", "<", add_days(getdate(to_date), 1)])
	if teacher:
		filters.append([MESSAGE_DOCTYPE, "teacher", "=", teacher])
	if student:
		filters.append([MESSAGE_DOCTYPE, "student", "=", student])
	if category:
		_valid_category(category)
		filters.append([MESSAGE_DOCTYPE, "category", "=", category])
	if status:
		if status not in {"Queued", "Sent", "Failed"}:
			frappe.throw(_("Message status is invalid."))
		filters.append([MESSAGE_DOCTYPE, "status", "=", status])

	rows = frappe.get_all(
		MESSAGE_DOCTYPE,
		filters=filters,
		fields=_message_fields(),
		order_by="creation desc",
		limit_page_length=0,
	)
	items = _enrich_message_rows(rows)
	query_text = str(query or "").strip().casefold()
	if query_text:
		items = [item for item in items if query_text in " ".join(
			str(item.get(field) or "") for field in ("name", "student", "student_name", "parent", "parent_name", "recipient_email", "teacher", "teacher_name", "message")
		).casefold()]
	if campus:
		items = [item for item in items if item.get("campus") == campus]

	total = len(items)
	start = max(cint(limit_start), 0)
	page_length = _limit(limit, 100, 200)
	return {
		"items": items[start : start + page_length],
		"total": total,
		"has_more": start + page_length < total,
		"categories": list(MESSAGE_CATEGORIES),
	}


def send_parent_classroom_message_job(parent_classroom_message, attempt_number):
	frappe.db.sql("select name from `tabParent Classroom Message` where name = %s for update", (parent_classroom_message,))
	doc = frappe.get_doc(MESSAGE_DOCTYPE, parent_classroom_message)
	attempt = next((row for row in doc.attempts or [] if cint(row.attempt_number) == cint(attempt_number)), None)
	if not attempt or attempt.status != "Queued" or doc.status != "Queued":
		return {"sent": False, "skipped": True}

	try:
		mail_result = sendmail_or_skip(
			action="teacher_parent_classroom_message",
			recipients=[doc.recipient_email],
			sender=_sender_label(attempt.sender_email),
			reply_to=attempt.reply_to_email,
			subject=_email_subject(doc),
			message=_email_body(doc),
			reference_doctype=MESSAGE_DOCTYPE,
			reference_name=doc.name,
			delayed=False,
		)
		if mail_result and isinstance(mail_result, dict) and mail_result.get("skipped"):
			_mark_attempt(doc, attempt, "Failed", error=mail_result.get("reason") or email_block_reason())
			return {"sent": False, "skipped": True, "reason": attempt.error_summary}
		queue_name = getattr(mail_result, "name", None)
		_mark_attempt(doc, attempt, "Sent", email_queue=queue_name)
		return {"sent": True, "message": doc.name, "recipient": doc.recipient_email}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS classroom parent message failed: {0}".format(doc.name))
		_mark_attempt(doc, attempt, "Failed", error="Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def _teacher_message_context(teacher, course_session, attendance_entry, student):
	if not course_session or not attendance_entry or not student:
		frappe.throw(_("Course Session, Attendance Entry, and Student are required."))
	session = _get_owned_session(course_session, teacher.name)
	attendance = frappe.db.get_value(
		ATTENDANCE_DOCTYPE,
		attendance_entry,
		["name", "course_session", "student", "status"],
		as_dict=True,
	)
	if not attendance or attendance.course_session != session.name or attendance.student != student:
		frappe.throw(_("The selected Student is not in this Course Session roster."), frappe.PermissionError)
	if attendance.status in {"Leave", "Cancelled"}:
		frappe.throw(_("A classroom message cannot be sent for a non-attending roster entry."))
	student_doc = frappe.get_doc("Student", student)
	parent_name = student_doc.get("guardian") or student_doc.get("parent")
	if not parent_name:
		frappe.throw(_("This Student is not linked to a Parent."))
	parent = frappe.get_doc("Parent", parent_name)
	recipient_email = _parent_email(parent)
	if not recipient_email:
		frappe.throw(_("The Parent email is missing. Ask School Admin to update the Parent before sending."))
	validate_email_address(recipient_email, throw=True)
	return {
		"session": session,
		"attendance": attendance,
		"student": student_doc,
		"parent": parent,
		"recipient_email": recipient_email.lower(),
	}


def _parent_email(parent):
	linked_user = str(parent.get("linked_user") or "").strip()
	if not linked_user:
		return ""
	return str(frappe.db.get_value("User", linked_user, "email") or linked_user).strip()


def _school_mail_context():
	settings = get_invoice_settings()
	school_email = str(settings.get("school_email") or "").strip()
	if not school_email:
		account = frappe.db.get_value(
			"Email Account",
			{"default_outgoing": 1, "enable_outgoing": 1},
			["email_id"],
			as_dict=True,
		)
		school_email = str((account or {}).get("email_id") or "").strip()
	if not school_email:
		frappe.throw(_("The school outgoing email is not configured."))
	validate_email_address(school_email, throw=True)
	return {
		"sender_email": school_email.lower(),
		"reply_to_email": school_email.lower(),
		"school_name": str(settings.get("school_name") or "Queensland Art School").strip(),
	}


def _append_attempt(doc, attempt_number, requested_by, mail_context):
	doc.append("attempts", {
		"attempt_number": attempt_number,
		"requested_by": requested_by,
		"requested_at": now_datetime(),
		"sender_email": mail_context["sender_email"],
		"reply_to_email": mail_context["reply_to_email"],
		"status": "Queued",
	})


def _queue_delivery_or_mark_failed(message_name, attempt_number):
	try:
		frappe.enqueue(
			"qas_custom.services.parent_classroom_messages.send_parent_classroom_message_job",
			queue="short",
			timeout=300,
			deduplicate=True,
			job_id="parent-classroom-message-{0}-{1}".format(_job_key(message_name), cint(attempt_number)),
			parent_classroom_message=message_name,
			attempt_number=cint(attempt_number),
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS classroom message queue failed: {0}".format(message_name))
		doc = frappe.get_doc(MESSAGE_DOCTYPE, message_name)
		attempt = next((row for row in doc.attempts or [] if cint(row.attempt_number) == cint(attempt_number)), None)
		if attempt:
			_mark_attempt(doc, attempt, "Failed", error="Email job could not be queued.")


def _mark_attempt(doc, attempt, status, email_queue=None, error=""):
	now = now_datetime()
	attempt.status = status
	attempt.email_queue = email_queue
	attempt.error_summary = str(error or "")[:500]
	attempt.sent_at = now if status == "Sent" else None
	doc.status = status
	doc.sent_at = now if status == "Sent" else None
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _message_payload(message_name):
	doc = frappe.get_doc(MESSAGE_DOCTYPE, message_name)
	return _enrich_message_rows([frappe._dict({field: doc.get(field) for field in _message_fields()})])[0]


def _message_fields():
	return [
		"name", "course_session", "attendance_entry", "student", "parent", "teacher", "category", "message",
		"recipient_email", "status", "attempt_count", "sent_at", "created_by_user", "creation", "modified",
	]


def _enrich_message_rows(rows):
	if not rows:
		return []
	message_names = [row.get("name") for row in rows]
	session_ids = {row.get("course_session") for row in rows if row.get("course_session")}
	sessions = {
		row.name: row for row in frappe.get_all(
			"Course Sessions",
			filters={"name": ["in", list(session_ids)]},
			fields=["name", "weekly_timeslot", "session_date"],
			limit_page_length=0,
		)
	} if session_ids else {}
	timeslot_ids = {row.get("weekly_timeslot") for row in sessions.values() if row.get("weekly_timeslot")}
	timeslots = {
		row.name: row for row in frappe.get_all(
			"Weekly Timeslot",
			filters={"name": ["in", list(timeslot_ids)]},
			fields=["name", "course", "campus"],
			limit_page_length=0,
		)
	} if timeslot_ids else {}
	student_map = _name_map("Student", {row.get("student") for row in rows}, "student_name")
	parent_map = _name_map("Parent", {row.get("parent") for row in rows}, "parent_name")
	teacher_map = _name_map("Teacher", {row.get("teacher") for row in rows}, "teacher_name")
	attempts = defaultdict(list)
	for attempt in frappe.get_all(
		ATTEMPT_DOCTYPE,
		filters={"parent": ["in", message_names], "parenttype": MESSAGE_DOCTYPE, "parentfield": "attempts"},
		fields=["parent", "attempt_number", "requested_by", "requested_at", "sender_email", "reply_to_email", "email_queue", "status", "sent_at", "error_summary"],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	):
		attempts[attempt.parent].append(dict(attempt))

	items = []
	for source in rows:
		item = dict(source)
		session = sessions.get(item.get("course_session")) or {}
		timeslot = timeslots.get(session.get("weekly_timeslot")) or {}
		item.update({
			"student_name": student_map.get(item.get("student")) or item.get("student"),
			"parent_name": parent_map.get(item.get("parent")) or item.get("parent"),
			"teacher_name": teacher_map.get(item.get("teacher")) or item.get("teacher"),
			"session_date": session.get("session_date"),
			"course": timeslot.get("course"),
			"campus": timeslot.get("campus"),
			"attempts": attempts.get(item.get("name"), []),
		})
		items.append(item)
	return items


def _name_map(doctype, names, label_field):
	names = [name for name in names if name]
	if not names:
		return {}
	return {
		row.name: row.get(label_field) or row.name
		for row in frappe.get_all(doctype, filters={"name": ["in", names]}, fields=["name", label_field], limit_page_length=0)
	}


def _get_owned_session(course_session, teacher_name):
	from qas_custom.services.teacher_portal import _get_owned_session as get_owned_session

	return get_owned_session(course_session, teacher_name)


def _require_teacher():
	from qas_custom.services.teacher_portal import _require_teacher as require_teacher

	return require_teacher()


def _require_school_admin():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	if not set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can access Parent Messages."), frappe.PermissionError)


def _valid_category(value):
	value = str(value or "").strip()
	if value not in MESSAGE_CATEGORIES:
		frappe.throw(_("Message category is invalid."))
	return value


def _valid_message(value):
	value = str(value or "").strip()
	if not value:
		frappe.throw(_("Message is required."))
	if len(value) > MAX_MESSAGE_LENGTH:
		frappe.throw(_("Message must be 2,000 characters or fewer."))
	return value


def _required_text(value, error, max_length=None):
	value = str(value or "").strip()
	if not value:
		frappe.throw(error)
	if max_length and len(value) > max_length:
		frappe.throw(_("Client request ID is too long."))
	return value


def _email_subject(doc):
	student_name = frappe.db.get_value("Student", doc.student, "student_name") or doc.student
	first_name = str(student_name or "Student").strip().split()[0]
	return _("A classroom message about {0}").format(first_name)


def _email_body(doc):
	student_name = frappe.db.get_value("Student", doc.student, "student_name") or doc.student
	parent_name = frappe.db.get_value("Parent", doc.parent, "parent_name") or "Parent"
	teacher_name = frappe.db.get_value("Teacher", doc.teacher, "teacher_name") or doc.teacher
	session = frappe.db.get_value("Course Sessions", doc.course_session, ["weekly_timeslot", "session_date"], as_dict=True) or {}
	timeslot = frappe.db.get_value("Weekly Timeslot", session.get("weekly_timeslot"), ["course"], as_dict=True) or {}
	message_html = escape_html(doc.message).replace("\n", "<br>")
	return """<div style="font-family:Arial,sans-serif;color:#172033;line-height:1.6">
<p>Dear {parent},</p>
<p>{teacher} has shared a classroom message about {student}.</p>
<p><strong>Course:</strong> {course}<br><strong>Class date:</strong> {date}<br><strong>Feedback type:</strong> {category}</p>
<div style="margin:16px 0;padding:14px;border-left:4px solid #ef6548;background:#f8fafc">{message}</div>
<p>If you would like to respond, please reply to this email. The Queensland Art School team will handle your reply.</p>
<p>Queensland Art School</p>
</div>""".format(
		parent=escape_html(parent_name),
		teacher=escape_html(teacher_name),
		student=escape_html(student_name),
		course=escape_html(timeslot.get("course") or "Class"),
		date=escape_html(str(session.get("session_date") or "")),
		category=escape_html(doc.category),
		message=message_html,
	)


def _sender_label(email):
	name = str(get_invoice_settings().get("school_name") or "Queensland Art School").replace("\n", " ").replace("\r", " ").strip()
	return "{0} <{1}>".format(name, email)


def _job_key(value):
	return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or ""))


def _limit(value, default, maximum):
	value = cint(value) or default
	return max(1, min(value, maximum))
