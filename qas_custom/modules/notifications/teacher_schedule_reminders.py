from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import escape_html, formatdate, get_datetime_in_timezone, now_datetime

from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, get_attendance_entries
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


BRISBANE_TIMEZONE = "Australia/Brisbane"
NON_ATTENDING_STATUSES = {"Leave", "Cancelled"}
TRIAL_ENROLLMENT_TYPE = "Trial"
MAKEUP_ENROLLMENT_TYPE = "Makeup"
EVENT_PREFIX = "teacher_next_day_schedule:"


def run_teacher_next_day_schedule_reminders():
	"""Queue tomorrow's schedule digest at 7 PM Brisbane time only."""
	now = get_datetime_in_timezone(BRISBANE_TIMEZONE)
	if now.hour != 19:
		return {"skipped": True, "reason": "Outside the 7 PM Australia/Brisbane send window."}
	return enqueue_teacher_next_day_schedule_reminders(now.date() + timedelta(days=1))


def enqueue_teacher_next_day_schedule_reminders(target_date):
	groups = get_teacher_next_day_schedule_groups(target_date)
	result = {"target_date": str(target_date), "queued": 0, "skipped": 0, "failed": 0}
	for teacher, sessions in groups.items():
		event_key = teacher_next_day_schedule_event_key(teacher, target_date)
		if _notification_event_exists(event_key):
			result["skipped"] += 1
			continue

		teacher_info = _get_teacher_info(teacher)
		subject = _teacher_schedule_subject(target_date)
		message = _teacher_schedule_message(target_date, sessions)
		log_name = _create_notification_log(event_key, teacher_info, subject, message)
		recipient = _teacher_email(teacher_info)
		if not recipient:
			_mark_notification_failed(log_name, "No teacher email found.")
			result["failed"] += 1
			continue
		if not outbound_email_enabled():
			_mark_notification_failed(log_name, email_block_reason())
			result["skipped"] += 1
			continue

		_mark_notification_queued(log_name)
		frappe.enqueue(
			"qas_custom.modules.notifications.teacher_schedule_reminders.send_teacher_next_day_schedule_reminder_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			teacher=teacher,
			target_date=str(target_date),
			notification_log=log_name,
		)
		result["queued"] += 1
	return result


def send_teacher_next_day_schedule_reminder_job(teacher, target_date, notification_log=None):
	target_date = str(target_date)
	sessions = get_teacher_next_day_schedule_groups(target_date).get(teacher, [])
	if not sessions:
		_mark_notification_failed(notification_log, "No eligible next-day classes remain.")
		return {"sent": False, "skipped": True, "reason": "No eligible next-day classes remain."}

	teacher_info = _get_teacher_info(teacher)
	recipient = _teacher_email(teacher_info)
	if not recipient:
		_mark_notification_failed(notification_log, "No teacher email found.")
		return {"sent": False, "skipped": True, "reason": "No teacher email found."}

	try:
		mail_result = sendmail_or_skip(
			action="teacher_next_day_schedule_reminder",
			recipients=[recipient],
			subject=_teacher_schedule_subject(target_date),
			message=_teacher_schedule_message(target_date, sessions),
			reference_doctype="Teacher",
			reference_name=teacher,
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason}
		_mark_notification_sent(notification_log)
		return {"sent": True, "recipient": recipient}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS teacher next-day schedule reminder failed: {0}".format(teacher))
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def get_teacher_next_day_schedule_groups(target_date):
	target_date = str(target_date)
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"session_date": target_date, "status": ["!=", "Cancelled"]},
		fields=["name", "weekly_timeslot", "teacher_override"],
		order_by="name asc",
	)
	timeslots = _get_timeslot_map([session.get("weekly_timeslot") for session in sessions])
	attendance = get_attendance_entries([session.get("name") for session in sessions], fields=["course_session", "enrollment_type", "status"])
	return _build_schedule_groups(sessions, timeslots, attendance)


def _build_schedule_groups(sessions, timeslots, attendance_rows):
	attendance_by_session = defaultdict(list)
	for row in attendance_rows:
		if (row.get("status") or "").strip() not in NON_ATTENDING_STATUSES:
			attendance_by_session[row.get("course_session")].append(row)

	groups = defaultdict(list)
	for session in sessions:
		timeslot = timeslots.get(session.get("weekly_timeslot"))
		if not timeslot:
			continue
		rows = attendance_by_session.get(session.get("name"), [])
		if not rows:
			continue
		teacher = session.get("teacher_override") or timeslot.get("teacher")
		if not teacher:
			continue
		groups[teacher].append(
			{
				"course": timeslot.get("course") or "Class",
				"campus": timeslot.get("campus") or "Not assigned",
				"start_time": _display_time(timeslot.get("start_time")),
				"end_time": _display_time(timeslot.get("end_time")),
				"student_count": len(rows),
				"trial_count": sum(row.get("enrollment_type") == TRIAL_ENROLLMENT_TYPE for row in rows),
				"makeup_count": sum(row.get("enrollment_type") == MAKEUP_ENROLLMENT_TYPE for row in rows),
			}
		)
	for sessions_for_teacher in groups.values():
		sessions_for_teacher.sort(key=lambda row: (row["start_time"], row["course"], row["campus"]))
	return dict(groups)


def teacher_next_day_schedule_event_key(teacher, target_date):
	identity = "\x1f".join((str(teacher or ""), str(target_date or "")))
	return "{0}{1}".format(EVENT_PREFIX, sha256(identity.encode()).hexdigest()[:24])


def _get_timeslot_map(names):
	names = list({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", names]},
		fields=["name", "course", "campus", "teacher", "start_time", "end_time"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _get_teacher_info(teacher):
	return frappe.db.get_value("Teacher", teacher, ["name", "teacher_name", "email", "user"], as_dict=True) or {"name": teacher}


def _teacher_email(teacher):
	for fieldname in ["email", "email_id", "contact_email"]:
		if teacher.get(fieldname):
			return str(teacher.get(fieldname)).strip().lower()
	user = teacher.get("user")
	if user and frappe.db.exists("User", user):
		return (frappe.db.get_value("User", user, "email") or user or "").strip().lower()
	return ""


def _teacher_schedule_subject(target_date):
	return _("Your classes for {0}").format(formatdate(target_date, "EEEE d MMMM yyyy"))


def _teacher_schedule_message(target_date, sessions):
	rows = []
	for session in sessions:
		rows.append(
			"<tr><td>{0}</td><td>{1}</td><td>{2} - {3}</td><td>{4}</td><td>{5}</td><td>{6}</td></tr>".format(
				escape_html(session["course"]),
				escape_html(session["campus"]),
				escape_html(session["start_time"]),
				escape_html(session["end_time"]),
				session["student_count"],
				session["trial_count"],
				session["makeup_count"],
			)
		)
	return "".join(
		[
			"<p>{0}</p>".format(_("Hello,")),
			"<p>{0}</p>".format(_("Here are your classes for {0}.").format(escape_html(formatdate(target_date, "EEEE d MMMM yyyy")))),
			"<table><thead><tr><th>{0}</th><th>{1}</th><th>{2}</th><th>{3}</th><th>{4}</th><th>{5}</th></tr></thead><tbody>{6}</tbody></table>".format(
				_("Course"), _("Campus"), _("Time"), _("Students"), _("Trial"), _("Makeup"), "".join(rows)
			),
			"<p>{0}</p>".format(_("Queensland Art School")),
		]
	)


def _display_time(value):
	text = str(value or "").strip()
	return text[:5] if len(text) >= 5 else text or "-"


def _notification_event_exists(event_key):
	if not frappe.db.exists("DocType", "Notification Log"):
		return False
	meta = frappe.get_meta("Notification Log")
	if meta.has_field("event_key"):
		return bool(frappe.db.exists("Notification Log", {"event_key": event_key}))
	return bool(frappe.db.exists("Notification Log", {"document_name": event_key}))


def _create_notification_log(event_key, teacher, subject, message):
	if not frappe.db.exists("DocType", "Notification Log"):
		return None
	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.type = "Alert"
	log.email_content = message
	log.document_type = "Teacher"
	log.document_name = teacher.get("name")
	log.from_user = frappe.session.user
	if log.meta.has_field("for_user"):
		log.for_user = teacher.get("user") or frappe.session.user
	for fieldname, value in {"event_key": event_key, "email_to": _teacher_email(teacher), "recipient_email": _teacher_email(teacher)}.items():
		if log.meta.has_field(fieldname):
			setattr(log, fieldname, value)
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	return log.name


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
	return {fieldname: status for fieldname in ["status", "delivery_status", "email_status"] if meta.has_field(fieldname)}
