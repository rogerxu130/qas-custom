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
		message = _teacher_schedule_message(target_date, sessions, teacher_name=teacher_info.get("teacher_name"))
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
			message=_teacher_schedule_message(target_date, sessions, teacher_name=teacher_info.get("teacher_name")),
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
	return _("Tomorrow's classes — {0}").format(formatdate(target_date, "EEEE d MMMM yyyy"))


def _teacher_schedule_message(target_date, sessions, teacher_name=None):
	date_display = escape_html(formatdate(target_date, "EEEE d MMMM yyyy"))
	teacher_name = str(teacher_name or "").strip()
	greeting = _("Hi {0},").format(escape_html(teacher_name)) if teacher_name else _("Hello,")
	class_count = len(sessions)
	class_summary = (
		_("You have {0} class.").format(class_count)
		if class_count == 1
		else _("You have {0} classes.").format(class_count)
	)
	cards = []
	for session in sessions:
		student_count = int(session.get("student_count") or 0)
		trial_count = int(session.get("trial_count") or 0)
		makeup_count = int(session.get("makeup_count") or 0)
		badges = []
		if trial_count > 0:
			badges.append(
				'<span style="display:inline-block;margin:4px 6px 0 0;padding:5px 9px;border-radius:999px;'
				'background-color:#fff0eb;color:#b94328;font-size:12px;font-weight:700;line-height:1.2;">{0}</span>'.format(
					escape_html(_count_label(trial_count, _("Trial"), _("Trials")))
				)
			)
		if makeup_count > 0:
			badges.append(
				'<span style="display:inline-block;margin:4px 6px 0 0;padding:5px 9px;border-radius:999px;'
				'background-color:#eaf3ff;color:#185ca8;font-size:12px;font-weight:700;line-height:1.2;">{0}</span>'.format(
					escape_html(_count_label(makeup_count, _("Makeup"), _("Makeups")))
				)
			)
		cards.append(
			"""
			<tr>
				<td style="padding:0 0 14px;">
					<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;border:1px solid #e2e8f0;border-radius:12px;background-color:#ffffff;">
						<tr>
							<td style="padding:18px 20px;">
								<div style="margin:0 0 6px;color:#e75f43;font-size:19px;font-weight:700;line-height:1.3;">{start_time} – {end_time}</div>
								<div style="margin:0 0 5px;color:#172033;font-size:18px;font-weight:700;line-height:1.35;">{course}</div>
								<div style="margin:0 0 12px;color:#64748b;font-size:14px;line-height:1.4;">{campus}</div>
								<div style="color:#334155;font-size:14px;line-height:1.4;">
									<span style="display:inline-block;margin:4px 10px 0 0;font-weight:700;">{students}</span>{badges}
								</div>
							</td>
						</tr>
					</table>
				</td>
			</tr>
			""".format(
				start_time=escape_html(str(session.get("start_time") or "-")),
				end_time=escape_html(str(session.get("end_time") or "-")),
				course=escape_html(str(session.get("course") or _("Class"))),
				campus=escape_html(str(session.get("campus") or _("Not assigned"))),
				students=escape_html(_count_label(student_count, _("student"), _("students"))),
				badges="".join(badges),
			)
		)

	return """
		<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#172033;">
			<tr>
				<td align="center" style="padding:24px 12px;">
					<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:640px;border-collapse:separate;background-color:#ffffff;border-radius:16px;overflow:hidden;">
						<tr>
							<td style="height:6px;background-color:#ef6a4c;font-size:0;line-height:0;">&nbsp;</td>
						</tr>
						<tr>
							<td style="padding:24px 26px;background-color:#172033;">
								<div style="margin:0 0 6px;color:#f7b6a4;font-size:12px;font-weight:700;letter-spacing:1px;line-height:1.3;text-transform:uppercase;">{school_name}</div>
								<div style="margin:0 0 6px;color:#ffffff;font-size:26px;font-weight:700;line-height:1.25;">{title}</div>
								<div style="color:#dbe4f0;font-size:15px;line-height:1.4;">{date_display}</div>
							</td>
						</tr>
						<tr>
							<td style="padding:24px 26px 10px;">
								<div style="margin:0 0 8px;color:#172033;font-size:18px;font-weight:700;line-height:1.4;">{greeting}</div>
								<div style="margin:0;color:#64748b;font-size:15px;line-height:1.55;">{intro} {class_summary}</div>
							</td>
						</tr>
						<tr>
							<td style="padding:12px 26px 10px;">
								<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;">{cards}</table>
							</td>
						</tr>
						<tr>
							<td style="padding:16px 26px 24px;border-top:1px solid #e2e8f0;color:#94a3b8;font-size:12px;line-height:1.5;text-align:center;">{school_name}</td>
						</tr>
					</table>
				</td>
			</tr>
		</table>
	""".format(
		school_name=escape_html(_("Queensland Art School")),
		title=escape_html(_("Tomorrow's Classes")),
		date_display=date_display,
		greeting=greeting,
		intro=escape_html(_("Here is your schedule for tomorrow.")),
		class_summary=escape_html(class_summary),
		cards="".join(cards),
	)


def _count_label(count, singular, plural):
	return _("{0} {1}").format(count, singular if count == 1 else plural)


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
