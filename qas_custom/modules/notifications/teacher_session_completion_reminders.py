from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import cint, escape_html, formatdate, get_datetime_in_timezone, now_datetime

from qas_custom.services.class_attendance import get_attendance_entries
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


BRISBANE_TIMEZONE = "Australia/Brisbane"
TEACHER_PORTAL_URL = "https://teacher.queenslandartschool.com/"
REMINDER_WINDOW_MINUTES = 15
NON_ATTENDING_STATUSES = {"Leave", "Cancelled"}
UNMARKED_ATTENDANCE_STATUS = "To be started"
EVENT_PREFIX = "teacher_session_completion:"
CONFIG_KEY = "qas_teacher_session_completion_reminder_enabled"


def run_teacher_session_completion_reminders(now=None):
	"""Queue one reminder per eligible Course Session near its Brisbane end time."""
	if not teacher_session_completion_reminder_enabled():
		return {"skipped": True, "reason": "Teacher session completion reminders are disabled."}
	if not outbound_email_enabled():
		return {"skipped": True, "reason": email_block_reason()}

	now = now or get_datetime_in_timezone(BRISBANE_TIMEZONE)
	sessions = _get_due_session_completion_states(now)
	result = {"eligible": len(sessions), "queued": 0, "skipped": 0, "failed": 0}

	for session in sessions:
		try:
			_outcome = _queue_session_completion_reminder(session)
			result[_outcome] += 1
		except Exception:
			result["failed"] += 1
			frappe.log_error(
				frappe.get_traceback(),
				"QAS teacher session completion reminder queue failed: {0}".format(session.get("name")),
			)
	return result


def _queue_session_completion_reminder(session):
	event_key = teacher_session_completion_event_key(session.get("name"))
	if _notification_event_exists(event_key):
		return "skipped"
	frappe.enqueue(
		"qas_custom.modules.notifications.teacher_session_completion_reminders.send_teacher_session_completion_reminder_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=event_key.replace(":", "-"),
		deduplicate=True,
		course_session=session.get("name"),
	)
	return "queued"


def send_teacher_session_completion_reminder_job(course_session, now=None):
	if not teacher_session_completion_reminder_enabled():
		return {"sent": False, "skipped": True, "reason": "Teacher session completion reminders are disabled."}
	if not outbound_email_enabled():
		return {"sent": False, "skipped": True, "reason": email_block_reason()}

	now = now or get_datetime_in_timezone(BRISBANE_TIMEZONE)
	session = _get_session_completion_state(course_session, now)
	if not session:
		return {"sent": False, "skipped": True, "reason": "Course Session is no longer eligible for a reminder."}

	event_key = teacher_session_completion_event_key(course_session)
	if _notification_event_exists(event_key):
		return {
			"sent": False,
			"skipped": True,
			"reason": "A reminder has already been recorded for this Course Session.",
		}

	teacher_info = _get_teacher_info(session.get("teacher")) if session.get("teacher") else {}
	recipient = _teacher_email(teacher_info)
	subject = _teacher_session_completion_subject(session)
	message = _teacher_session_completion_message(session, teacher_name=teacher_info.get("teacher_name"))
	try:
		notification_log = _create_notification_log(
			event_key,
			course_session,
			teacher_info,
			subject,
			message,
			recipient,
		)
	except frappe.DuplicateEntryError:
		return {
			"sent": False,
			"skipped": True,
			"reason": "A reminder has already been recorded for this Course Session.",
		}
	if not notification_log:
		return {
			"sent": False,
			"skipped": True,
			"reason": "Notification Log is unavailable; the email was not sent without an idempotency reservation.",
		}

	if not session.get("teacher"):
		return _skip_job(notification_log, "No teacher assigned to this Course Session.")
	if not recipient:
		return _skip_job(notification_log, "No teacher email found.")

	_mark_notification_queued(notification_log)
	try:
		mail_result = sendmail_or_skip(
			action="teacher_session_completion_reminder",
			recipients=[recipient],
			subject=subject,
			message=message,
			reference_doctype="Course Sessions",
			reference_name=course_session,
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			return _skip_job(notification_log, mail_result.get("reason") or email_block_reason())
		_mark_notification_sent(notification_log)
		return {"sent": True, "recipient": recipient}
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"QAS teacher session completion reminder failed: {0}".format(course_session),
		)
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def teacher_session_completion_reminder_enabled():
	value = frappe.conf.get(CONFIG_KEY)
	return True if value is None else cint(value) != 0


def _get_due_session_completion_states(now):
	today = str(now.date())
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"session_date": today, "status": ["!=", "Cancelled"]},
		fields=["name", "weekly_timeslot", "session_date", "status", "teacher_override"],
		order_by="name asc",
		limit_page_length=0,
	)
	timeslots = _get_timeslot_map([session.get("weekly_timeslot") for session in sessions])
	due_sessions = _select_sessions_in_send_window(sessions, timeslots, now)
	if not due_sessions:
		return []

	session_ids = [session.get("name") for session in due_sessions]
	attendance = get_attendance_entries(session_ids, fields=["course_session", "status"])
	published_sessions = {
		row.get("course_session")
		for row in frappe.get_all(
			"Session Photo Post",
			filters={"course_session": ["in", session_ids], "status": "Published"},
			fields=["course_session"],
			limit_page_length=0,
		)
	}
	return _build_completion_states(due_sessions, timeslots, attendance, published_sessions)


def _get_session_completion_state(course_session, now):
	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "status", "teacher_override"],
		as_dict=True,
	)
	if not session or session.get("status") == "Cancelled":
		return None
	timeslots = _get_timeslot_map([session.get("weekly_timeslot")])
	due_sessions = _select_sessions_in_send_window([session], timeslots, now)
	if not due_sessions:
		return None
	attendance = get_attendance_entries([course_session], fields=["course_session", "status"])
	published_sessions = set(
		frappe.get_all(
			"Session Photo Post",
			filters={"course_session": course_session, "status": "Published"},
			pluck="course_session",
			limit_page_length=1,
		)
	)
	states = _build_completion_states(due_sessions, timeslots, attendance, published_sessions)
	return states[0] if states else None


def _select_sessions_in_send_window(sessions, timeslots, now):
	due = []
	for session in sessions:
		timeslot = timeslots.get(session.get("weekly_timeslot"))
		if not timeslot or not _is_in_send_window(now, session.get("session_date"), timeslot.get("end_time")):
			continue
		item = dict(session)
		item["session_end"] = _session_end_datetime(session.get("session_date"), timeslot.get("end_time"))
		due.append(item)
	return due


def _build_completion_states(sessions, timeslots, attendance_rows, published_sessions):
	attendance_by_session = defaultdict(list)
	for row in attendance_rows:
		attendance_by_session[row.get("course_session")].append(row)

	states = []
	for session in sessions:
		timeslot = timeslots.get(session.get("weekly_timeslot"))
		if not timeslot:
			continue
		expected_rows = [
			row
			for row in attendance_by_session.get(session.get("name"), [])
			if (row.get("status") or "").strip() not in NON_ATTENDING_STATUSES
		]
		if not expected_rows:
			continue

		needs_attendance = any(
			(row.get("status") or "").strip() == UNMARKED_ATTENDANCE_STATUS for row in expected_rows
		)
		needs_photos = session.get("name") not in published_sessions
		if not needs_attendance and not needs_photos:
			continue

		state = dict(session)
		state.update(
			{
				"course": timeslot.get("course") or "Class",
				"campus": timeslot.get("campus") or "Not assigned",
				"classroom": timeslot.get("classroom") or "Not assigned",
				"start_time": _display_time(timeslot.get("start_time")),
				"end_time": _display_time(timeslot.get("end_time")),
				"teacher": session.get("teacher_override") or timeslot.get("teacher"),
				"expected_student_count": len(expected_rows),
				"needs_attendance": needs_attendance,
				"needs_photos": needs_photos,
			}
		)
		states.append(state)
	return states


def _is_in_send_window(now, session_date, end_time):
	end_at = _session_end_datetime(session_date, end_time)
	if not end_at:
		return False
	local_now = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
	seconds_remaining = (end_at - local_now).total_seconds()
	return 0 < seconds_remaining <= REMINDER_WINDOW_MINUTES * 60


def _session_end_datetime(session_date, end_time):
	if not session_date or end_time in (None, ""):
		return None
	try:
		date_value = datetime.strptime(str(session_date)[:10], "%Y-%m-%d").date()
		time_value = _coerce_time(end_time)
		return datetime.combine(date_value, time_value) if time_value else None
	except (TypeError, ValueError):
		return None


def _coerce_time(value):
	if isinstance(value, time):
		return value.replace(tzinfo=None)
	if isinstance(value, timedelta):
		seconds = int(value.total_seconds()) % (24 * 60 * 60)
		return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
	text = str(value or "").strip().split(".", 1)[0]
	for pattern in ("%H:%M:%S", "%H:%M"):
		try:
			return datetime.strptime(text, pattern).time()
		except ValueError:
			continue
	return None


def teacher_session_completion_event_key(course_session):
	digest = sha256(str(course_session or "").encode()).hexdigest()[:24]
	return "{0}{1}".format(EVENT_PREFIX, digest)


def _teacher_session_completion_subject(session):
	return _("Action needed before class ends: {0} — {1}").format(
		session.get("course") or _("Class"),
		session.get("end_time") or "-",
	)


def _teacher_session_completion_message(session, teacher_name=None):
	teacher_name = str(teacher_name or "").strip()
	greeting = _("Hi {0},").format(escape_html(teacher_name)) if teacher_name else _("Hello,")
	actions = []
	if session.get("needs_attendance"):
		actions.append(_("Attendance is not complete. Please mark every expected student."))
	if session.get("needs_photos"):
		actions.append(_("Class photos have not been published. Please publish the class photos."))
	action_items = "".join(
		'<li style="margin:0 0 10px;color:#334155;font-size:15px;line-height:1.5;">{0}</li>'.format(
			escape_html(action)
		)
		for action in actions
	)
	date_display = escape_html(formatdate(session.get("session_date"), "EEEE d MMMM yyyy"))
	location = "{0} · {1}".format(session.get("campus") or _("Not assigned"), session.get("classroom") or _("Not assigned"))

	return """
		<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#172033;">
			<tr><td align="center" style="padding:24px 12px;">
				<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:620px;background-color:#ffffff;border-radius:16px;overflow:hidden;">
					<tr><td style="height:6px;background-color:#ef6a4c;font-size:0;line-height:0;">&nbsp;</td></tr>
					<tr><td style="padding:24px 26px;background-color:#172033;">
						<div style="margin:0 0 6px;color:#f7b6a4;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;">{school_name}</div>
						<div style="color:#ffffff;font-size:25px;font-weight:700;line-height:1.25;">{title}</div>
					</td></tr>
					<tr><td style="padding:24px 26px 8px;">
						<div style="margin:0 0 12px;font-size:18px;font-weight:700;">{greeting}</div>
						<div style="margin:0 0 6px;color:#e75f43;font-size:18px;font-weight:700;">{time}</div>
						<div style="margin:0 0 5px;font-size:18px;font-weight:700;">{course}</div>
						<div style="color:#64748b;font-size:14px;line-height:1.5;">{date}<br>{location}</div>
					</td></tr>
					<tr><td style="padding:12px 26px 8px;"><ul style="margin:0;padding-left:22px;">{actions}</ul></td></tr>
					<tr><td align="center" style="padding:14px 26px 30px;">
						<a href="{portal_url}" style="display:inline-block;padding:13px 22px;border-radius:9px;background-color:#ef6a4c;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;">{button}</a>
					</td></tr>
					<tr><td style="padding:16px 26px;border-top:1px solid #e2e8f0;color:#94a3b8;font-size:12px;text-align:center;">{school_name}</td></tr>
				</table>
			</td></tr>
		</table>
	""".format(
		school_name=escape_html(_("Queensland Art School")),
		title=escape_html(_("Complete Attendance & Class Photos")),
		greeting=greeting,
		time=escape_html("{0} – {1}".format(session.get("start_time") or "-", session.get("end_time") or "-")),
		course=escape_html(str(session.get("course") or _("Class"))),
		date=date_display,
		location=escape_html(location),
		actions=action_items,
		portal_url=TEACHER_PORTAL_URL,
		button=escape_html(_("Open Teacher Portal")),
	)


def _get_timeslot_map(names):
	names = list({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", names]},
		fields=["name", "course", "campus", "classroom", "teacher", "start_time", "end_time"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _get_teacher_info(teacher):
	return frappe.db.get_value(
		"Teacher",
		teacher,
		["name", "teacher_name", "email", "user"],
		as_dict=True,
	) or {"name": teacher}


def _teacher_email(teacher):
	for fieldname in ["email", "email_id", "contact_email"]:
		if teacher.get(fieldname):
			return str(teacher.get(fieldname)).strip().lower()
	user = teacher.get("user")
	if user and frappe.db.exists("User", user):
		return (frappe.db.get_value("User", user, "email") or user or "").strip().lower()
	return ""


def _display_time(value):
	coerced = _coerce_time(value)
	return coerced.strftime("%H:%M") if coerced else "-"


def _notification_event_exists(event_key):
	if not frappe.db.exists("DocType", "Notification Log"):
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _create_notification_log(event_key, course_session, teacher, subject, message, recipient):
	if not frappe.db.exists("DocType", "Notification Log"):
		return None
	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.type = "Alert"
	log.email_content = message
	log.document_type = "Course Sessions"
	log.document_name = course_session
	log.from_user = frappe.session.user
	if log.meta.has_field("for_user"):
		log.for_user = teacher.get("user") or frappe.session.user
	for fieldname, value in {
		"event_key": event_key,
		"email_to": recipient,
		"recipient_email": recipient,
	}.items():
		if log.meta.has_field(fieldname):
			setattr(log, fieldname, value)
	if not log.meta.has_field("event_key"):
		# Legacy fallback: keep the idempotency key in the standard reference field.
		log.document_name = event_key
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	return log.name


def _skip_job(log_name, reason):
	_mark_notification_failed(log_name, reason)
	return {"sent": False, "skipped": True, "reason": reason}


def _mark_notification_queued(log_name):
	_mark_notification_status(log_name, "Queued")


def _mark_notification_sent(log_name):
	_mark_notification_status(log_name, "Sent", sent_at=now_datetime())


def _mark_notification_failed(log_name, reason):
	if not log_name:
		return
	values = _notification_status_values("Failed")
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["failure_reason", "error", "error_message"]:
		if meta.has_field(fieldname):
			values[fieldname] = reason
			break
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _mark_notification_status(log_name, status, sent_at=None):
	if not log_name:
		return
	values = _notification_status_values(status)
	meta = frappe.get_meta("Notification Log")
	if sent_at and meta.has_field("sent_at"):
		values["sent_at"] = sent_at
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _notification_status_values(status):
	meta = frappe.get_meta("Notification Log")
	return {
		fieldname: status
		for fieldname in ["status", "delivery_status", "email_status"]
		if meta.has_field(fieldname)
	}
