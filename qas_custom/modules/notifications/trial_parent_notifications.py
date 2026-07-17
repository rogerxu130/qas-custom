from __future__ import annotations

from datetime import datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_datetime_in_timezone, get_system_timezone

from qas_custom.modules.notifications.commands import (
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
	_trial_class_reminder_context,
	_trial_class_reminder_email_message,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


BRISBANE_TIMEZONE = "Australia/Brisbane"
ACTIVE_TRIAL_STATUSES = {"Booked", "Rescheduled"}
BOOKING_CONFIG_KEY = "qas_trial_parent_booking_email_enabled"
REMINDER_CONFIG_KEY = "qas_trial_parent_24h_reminder_enabled"
REMINDER_MIN_SECONDS = 23 * 60 * 60 + 45 * 60
REMINDER_MAX_SECONDS = 24 * 60 * 60
EVENT_PREFIXES = {
	"booked": "trial_parent_booked:",
	"rescheduled": "trial_parent_rescheduled:",
	"reminder_24h": "trial_parent_24h:",
}


def queue_trial_parent_booking_change(inquiry_doc, old_doc=None):
	"""Queue one automatic parent confirmation for a meaningful Trial booking change."""
	event_kind = classify_trial_booking_change(inquiry_doc, old_doc)
	if not event_kind:
		return {"queued": False, "skipped": True, "reason": "No automatic Trial parent email is required."}
	if not booking_email_enabled():
		return {"queued": False, "skipped": True, "reason": "Automatic Trial booking emails are disabled."}
	if not outbound_email_enabled():
		return {"queued": False, "skipped": True, "reason": email_block_reason()}

	event_key = trial_parent_event_key(event_kind, inquiry_doc.name, inquiry_doc.course_session)
	if _notification_event_exists(event_key):
		return {"queued": False, "skipped": True, "duplicate": True}

	frappe.enqueue(
		"qas_custom.modules.notifications.trial_parent_notifications.send_trial_parent_booking_email_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=event_key.replace(":", "-"),
		deduplicate=True,
		inquiry=inquiry_doc.name,
		course_session=inquiry_doc.course_session,
		event_kind=event_kind,
	)
	return {"queued": True, "event_kind": event_kind}


def classify_trial_booking_change(inquiry_doc, old_doc=None):
	if inquiry_doc.get("inquiry_type") != "Trial Lesson" or not inquiry_doc.get("course_session"):
		return None
	status = (inquiry_doc.get("status") or "").strip()
	if status not in ACTIVE_TRIAL_STATUSES:
		return None

	old_status = ((old_doc.get("status") if old_doc else None) or "").strip()
	old_course_session = ((old_doc.get("course_session") if old_doc else None) or "").strip()
	if status == "Booked" and old_status not in ACTIVE_TRIAL_STATUSES:
		return "booked"
	if old_course_session != inquiry_doc.get("course_session") or (
		status == "Rescheduled" and old_status != "Rescheduled"
	):
		return "rescheduled"
	return None


def send_trial_parent_booking_email_job(inquiry, course_session, event_kind):
	if not booking_email_enabled():
		return _skipped("Automatic Trial booking emails are disabled.")
	if not outbound_email_enabled():
		return _skipped(email_block_reason())
	if event_kind not in {"booked", "rescheduled"}:
		return _skipped("Unsupported automatic Trial parent email event.")

	doc = _get_current_trial_inquiry(inquiry, course_session)
	if not doc:
		return _skipped("The Trial Inquiry is no longer booked into this Course Session.")
	if event_kind == "booked" and doc.get("status") != "Booked":
		return _skipped("The original Trial booking email is stale after a status change.")

	event_key = trial_parent_event_key(event_kind, inquiry, course_session)
	if _notification_event_exists(event_key):
		return _skipped("This automatic Trial parent email was already recorded.")

	return _send_automatic_trial_parent_email(doc, event_kind, event_key)


def run_trial_parent_24h_reminders(now=None):
	if not reminder_email_enabled():
		return {"skipped": True, "reason": "Automatic Trial 24-hour reminders are disabled."}
	if not outbound_email_enabled():
		return {"skipped": True, "reason": email_block_reason()}

	now = now or get_datetime_in_timezone(BRISBANE_TIMEZONE)
	candidates = _get_24h_candidates(now)
	event_keys = {
		candidate["name"]: trial_parent_event_key(
			"reminder_24h",
			candidate["name"],
			candidate["course_session"],
		)
		for candidate in candidates
	}
	existing_event_keys = _existing_notification_events(event_keys.values())
	result = {"eligible": len(candidates), "queued": 0, "skipped": 0, "failed": 0}
	for candidate in candidates:
		try:
			event_key = event_keys[candidate["name"]]
			if event_key in existing_event_keys:
				result["skipped"] += 1
				continue
			frappe.enqueue(
				"qas_custom.modules.notifications.trial_parent_notifications.send_trial_parent_24h_reminder_job",
				queue="short",
				timeout=300,
				enqueue_after_commit=True,
				job_id=event_key.replace(":", "-"),
				deduplicate=True,
				inquiry=candidate["name"],
				course_session=candidate["course_session"],
			)
			result["queued"] += 1
		except Exception:
			result["failed"] += 1
			frappe.log_error(frappe.get_traceback(), "QAS Trial parent 24-hour reminder queue failed")
	return result


def send_trial_parent_24h_reminder_job(inquiry, course_session, now=None):
	if not reminder_email_enabled():
		return _skipped("Automatic Trial 24-hour reminders are disabled.")
	if not outbound_email_enabled():
		return _skipped(email_block_reason())

	now = now or get_datetime_in_timezone(BRISBANE_TIMEZONE)
	doc = _get_current_trial_inquiry(inquiry, course_session)
	if not doc:
		return _skipped("The Trial Inquiry is no longer booked into this Course Session.")
	class_start = _get_class_start(course_session)
	if not class_start or not _is_24h_window(now, class_start):
		return _skipped("The Trial class is outside the 24-hour reminder window.")
	if _was_booked_or_rescheduled_inside_24h(inquiry, course_session, class_start):
		return _skipped("The Trial class was booked or rescheduled with less than 24 hours remaining.")

	event_key = trial_parent_event_key("reminder_24h", inquiry, course_session)
	if _notification_event_exists(event_key):
		return _skipped("This Trial 24-hour reminder was already recorded.")
	return _send_automatic_trial_parent_email(doc, "reminder_24h", event_key)


def booking_email_enabled():
	return _config_enabled(BOOKING_CONFIG_KEY)


def reminder_email_enabled():
	return _config_enabled(REMINDER_CONFIG_KEY)


def _config_enabled(key):
	value = frappe.conf.get(key)
	return True if value is None else cint(value) != 0


def _get_24h_candidates(now):
	target_date = (now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now) + timedelta(hours=24)
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"session_date": target_date.date()},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		limit_page_length=0,
	)
	session_map = {row.get("name"): row for row in sessions if row.get("status") != "Cancelled"}
	timeslot_map = _get_timeslot_map([row.get("weekly_timeslot") for row in session_map.values()])
	due_sessions = []
	for session in session_map.values():
		timeslot = timeslot_map.get(session.get("weekly_timeslot"))
		class_start = _session_start_datetime(session.get("session_date"), (timeslot or {}).get("start_time"))
		if class_start and _is_24h_window(now, class_start):
			due_sessions.append(session.get("name"))
	if not due_sessions:
		return []

	inquiries = frappe.get_all(
		"Inquiry",
		filters={
			"inquiry_type": "Trial Lesson",
			"status": ["in", sorted(ACTIVE_TRIAL_STATUSES)],
			"course_session": ["in", due_sessions],
		},
		fields=["name", "course_session"],
		limit_page_length=0,
	)
	return [dict(row) for row in inquiries]


def _get_current_trial_inquiry(inquiry, course_session):
	doc = frappe.db.get_value(
		"Inquiry",
		inquiry,
		["name", "inquiry_type", "status", "course_session"],
		as_dict=True,
	)
	if not doc or doc.get("inquiry_type") != "Trial Lesson":
		return None
	if doc.get("status") not in ACTIVE_TRIAL_STATUSES or doc.get("course_session") != course_session:
		return None
	return frappe.get_doc("Inquiry", inquiry)


def _send_automatic_trial_parent_email(inquiry_doc, event_kind, event_key):
	try:
		context = _trial_class_reminder_context(inquiry_doc)
	except Exception as exc:
		_reserve_failed_event(event_key, inquiry_doc.name, str(exc))
		return {"sent": False, "reason": str(exc)}

	subject, heading, intro = _email_copy(event_kind, context)
	message = _trial_class_reminder_email_message(context, heading=heading, intro=intro)
	try:
		log_name = _reserve_notification_event(
			event_key,
			context["recipient"],
			subject,
			message,
			inquiry_doc.name,
		)
	except frappe.DuplicateEntryError:
		return _skipped("This automatic Trial parent email was already recorded.")
	if not log_name:
		return _skipped("Notification Log is unavailable; email was not sent without an idempotency reservation.")

	_mark_notification_queued(log_name)
	try:
		mail_result = sendmail_or_skip(
			action="trial_parent_{0}".format(event_kind),
			recipients=[context["recipient"]["email"]],
			subject=subject,
			message=message,
			reference_doctype="Inquiry",
			reference_name=inquiry_doc.name,
			reply_to=context["school_email"],
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(log_name, reason)
			return _skipped(reason)
		_mark_notification_sent(log_name)
		return {"sent": True, "recipient": context["recipient"]["email"]}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS automatic Trial parent email failed: {0}".format(inquiry_doc.name))
		_mark_notification_failed(log_name, "Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def _email_copy(event_kind, context):
	student = context["student_name"]
	date_display = context["date_display"]
	if event_kind == "booked":
		return (
			_("Trial Class Booked: {0} — {1}").format(student, date_display),
			_("Trial Class Booked"),
			_("{0}'s trial class has been booked successfully.").format(student),
		)
	if event_kind == "rescheduled":
		return (
			_("Trial Class Rescheduled: {0} — {1}").format(student, date_display),
			_("Trial Class Rescheduled"),
			_("{0}'s trial class has been rescheduled successfully. Please use the updated class details below.").format(student),
		)
	return (
		_("Reminder: {0}'s Trial Class on {1}").format(student, date_display),
		_("Trial class reminder"),
		_("This is a friendly reminder about your child's upcoming trial class."),
	)


def trial_parent_event_key(event_kind, inquiry, course_session):
	identity = "\x1f".join((str(inquiry or ""), str(course_session or ""), str(event_kind or "")))
	digest = sha256(identity.encode()).hexdigest()[:24]
	return "{0}{1}".format(EVENT_PREFIXES[event_kind], digest)


def _notification_event_exists(event_key):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _existing_notification_events(event_keys):
	event_keys = list({key for key in event_keys if key})
	if not event_keys or not _notification_log_available():
		return set()
	meta = frappe.get_meta("Notification Log")
	fieldname = "event_key" if meta.has_field("event_key") else "document_name"
	return set(
		frappe.get_all(
			"Notification Log",
			filters={fieldname: ["in", event_keys]},
			pluck=fieldname,
			limit_page_length=0,
		)
	)


def _reserve_notification_event(event_key, recipient, subject, message, inquiry):
	"""Atomically reserve an automatic email event before delivery."""
	if not _notification_log_available():
		return None
	lock_name = "qas-trial-parent-email:{0}".format(event_key)
	with frappe.cache.lock(lock_name, timeout=30, blocking_timeout=10):
		if _notification_event_exists(event_key):
			raise frappe.DuplicateEntryError

		log = frappe.new_doc("Notification Log")
		log.subject = subject
		log.type = "Alert"
		log.email_content = message
		log.document_type = "Inquiry"
		log.document_name = inquiry
		log.from_user = frappe.session.user
		if log.meta.has_field("for_user"):
			log.for_user = recipient.get("for_user") or frappe.session.user
		for fieldname, value in {
			"event_key": event_key,
			"email_to": recipient.get("email"),
			"recipient_email": recipient.get("email"),
		}.items():
			if log.meta.has_field(fieldname):
				setattr(log, fieldname, value)
		if not log.meta.has_field("event_key"):
			# Standard Notification Log has no event_key field. Persist the stable
			# idempotency key in its indexed document_name field instead.
			log.document_name = event_key
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)
		return log.name


def _was_booked_or_rescheduled_inside_24h(inquiry, course_session, class_start):
	if not _notification_log_available():
		return False
	keys = [
		trial_parent_event_key("booked", inquiry, course_session),
		trial_parent_event_key("rescheduled", inquiry, course_session),
	]
	meta = frappe.get_meta("Notification Log")
	fieldname = "event_key" if meta.has_field("event_key") else "document_name"
	rows = frappe.get_all(
		"Notification Log",
		filters={fieldname: ["in", keys]},
		fields=["creation"],
		order_by="creation desc",
		limit_page_length=1,
	)
	if not rows:
		return False
	created_brisbane = _system_datetime_to_brisbane(rows[0].get("creation"))
	return bool(created_brisbane and created_brisbane >= class_start - timedelta(hours=24))


def _reserve_failed_event(event_key, inquiry, reason):
	try:
		log_name = _reserve_notification_event(
			event_key,
			{"email": ""},
			_("Automatic Trial parent email could not be prepared"),
			reason,
			inquiry,
		)
		_mark_notification_failed(log_name, reason)
	except frappe.DuplicateEntryError:
		pass


def _get_class_start(course_session):
	sessions = _get_session_map([course_session])
	session = sessions.get(course_session)
	if not session or session.get("status") == "Cancelled":
		return None
	timeslot = _get_timeslot_map([session.get("weekly_timeslot")]).get(session.get("weekly_timeslot"))
	return _session_start_datetime(session.get("session_date"), (timeslot or {}).get("start_time"))


def _get_session_map(names):
	names = list({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Course Sessions",
		filters={"name": ["in", names]},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _get_timeslot_map(names):
	names = list({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", names]},
		fields=["name", "start_time"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _session_start_datetime(session_date, start_time):
	if not session_date or start_time in (None, ""):
		return None
	try:
		date_value = datetime.strptime(str(session_date)[:10], "%Y-%m-%d").date()
		time_value = _coerce_time(start_time)
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


def _is_24h_window(now, class_start):
	local_now = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
	seconds_remaining = (class_start - local_now).total_seconds()
	return REMINDER_MIN_SECONDS <= seconds_remaining <= REMINDER_MAX_SECONDS


def _system_datetime_to_brisbane(value):
	if not value:
		return None
	datetime_value = get_datetime(value)
	source_timezone = ZoneInfo(get_system_timezone())
	if datetime_value.tzinfo is None:
		datetime_value = datetime_value.replace(tzinfo=source_timezone)
	return datetime_value.astimezone(ZoneInfo(BRISBANE_TIMEZONE)).replace(tzinfo=None)


def _skipped(reason):
	return {"sent": False, "skipped": True, "reason": reason}
