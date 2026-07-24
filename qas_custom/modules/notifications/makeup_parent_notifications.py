from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import escape_html, formatdate, get_time, getdate

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.presentation import DEFAULT_PARENT_PORTAL_URL
from qas_custom.modules.notifications.commands import (
	_mark_notification_failed,
	_mark_notification_queued,
	_mark_notification_sent,
	_notification_log_available,
)
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.display_labels import get_makeup_voucher_label
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


EVENT_PREFIXES = {
	"voucher_issued": "makeup_parent_voucher:",
	"booking_confirmed": "makeup_parent_booking:",
}


def queue_makeup_voucher_issued_email(voucher: str):
	return _queue_makeup_parent_email("voucher_issued", voucher=voucher)


def queue_makeup_booking_confirmation(voucher: str, course_session: str, student: str):
	return _queue_makeup_parent_email(
		"booking_confirmed",
		voucher=voucher,
		course_session=course_session,
		student=student,
	)


def _queue_makeup_parent_email(
	event_kind: str,
	*,
	voucher: str,
	course_session: str | None = None,
	student: str | None = None,
):
	if event_kind not in EVENT_PREFIXES:
		return {"queued": False, "skipped": True, "reason": "Unsupported makeup parent email event."}
	event_key = makeup_parent_event_key(event_kind, voucher, course_session, student)
	try:
		if _notification_event_exists(event_key):
			return {"queued": False, "skipped": True, "duplicate": True, "event_key": event_key}

		context = _build_makeup_parent_context(
			event_kind,
			voucher=voucher,
			course_session=course_session,
			student=student,
		)
		subject = makeup_parent_email_subject(context)
		message = _makeup_parent_email_message(context)
		try:
			log_name = _reserve_notification_event(event_key, context, subject, message)
		except frappe.DuplicateEntryError:
			return {"queued": False, "skipped": True, "duplicate": True, "event_key": event_key}
		if not log_name:
			return {
				"queued": False,
				"skipped": True,
				"reason": "Notification Log is unavailable; email was not queued without an idempotency reservation.",
				"event_key": event_key,
			}

		recipient = context["recipient"].get("email")
		if not recipient:
			_mark_notification_failed(log_name, "No parent email found.")
			return {
				"queued": False,
				"reason": "No parent email found.",
				"notification_log": log_name,
				"event_key": event_key,
			}

		_mark_notification_queued(log_name)
		if not outbound_email_enabled():
			reason = email_block_reason()
			_mark_notification_failed(log_name, reason)
			return {
				"queued": False,
				"skipped": True,
				"reason": reason,
				"notification_log": log_name,
				"event_key": event_key,
			}

		try:
			frappe.enqueue(
				"qas_custom.modules.notifications.makeup_parent_notifications.send_makeup_parent_email_job",
				queue="short",
				timeout=300,
				enqueue_after_commit=True,
				job_id=event_key.replace(":", "-"),
				deduplicate=True,
				event_kind=event_kind,
				voucher=voucher,
				course_session=course_session,
				student=student,
				notification_log=log_name,
			)
		except Exception:
			_mark_notification_failed(log_name, "Email job could not be queued.")
			frappe.log_error(
				frappe.get_traceback(),
				"QAS makeup parent email queue failed: {0}".format(voucher),
			)
			return {
				"queued": False,
				"reason": "Email job could not be queued.",
				"notification_log": log_name,
				"event_key": event_key,
			}
		return {
			"queued": True,
			"recipient": recipient,
			"notification_log": log_name,
			"event_key": event_key,
		}
	except Exception:
		try:
			frappe.log_error(
				frappe.get_traceback(),
				"QAS makeup parent email could not be prepared: {0}".format(voucher),
			)
		except Exception:
			pass
		return {"queued": False, "reason": "Parent email could not be prepared.", "event_key": event_key}


def send_makeup_parent_email_job(
	*,
	event_kind: str,
	voucher: str,
	course_session: str | None = None,
	student: str | None = None,
	notification_log: str | None = None,
):
	if _notification_log_sent(notification_log):
		return {"sent": False, "skipped": True, "duplicate": True}
	if not outbound_email_enabled():
		reason = email_block_reason()
		_mark_notification_failed(notification_log, reason)
		return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}
	if not _makeup_event_is_current(event_kind, voucher, course_session, student):
		reason = "The makeup notification is no longer current."
		_mark_notification_failed(notification_log, reason)
		return {"sent": False, "skipped": True, "reason": reason, "notification_log": notification_log}

	try:
		context = _build_makeup_parent_context(
			event_kind,
			voucher=voucher,
			course_session=course_session,
			student=student,
		)
		recipient = context["recipient"].get("email")
		if not recipient:
			_mark_notification_failed(notification_log, "No parent email found.")
			return {"sent": False, "reason": "No parent email found.", "notification_log": notification_log}

		subject = makeup_parent_email_subject(context)
		message = _makeup_parent_email_message(context)
		_refresh_notification_log(notification_log, context, subject, message)
		mail_kwargs = {
			"action": "makeup_parent_{0}".format(event_kind),
			"recipients": [recipient],
			"subject": subject,
			"message": message,
			"reference_doctype": "Makeup Voucher",
			"reference_name": voucher,
			"delayed": False,
		}
		if context.get("school_email"):
			mail_kwargs["reply_to"] = context["school_email"]
		mail_result = sendmail_or_skip(**mail_kwargs)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(notification_log, reason)
			return {
				"sent": False,
				"skipped": True,
				"reason": reason,
				"notification_log": notification_log,
			}
		_mark_notification_sent(notification_log)
		return {
			"sent": True,
			"recipient": recipient,
			"notification_log": notification_log,
		}
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"QAS makeup parent email failed: {0}".format(voucher),
		)
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed.", "notification_log": notification_log}


def makeup_parent_event_key(
	event_kind: str,
	voucher: str,
	course_session: str | None = None,
	student: str | None = None,
):
	if event_kind not in EVENT_PREFIXES:
		raise ValueError("Unsupported makeup parent email event.")
	identity = "\x1f".join(
		(str(event_kind or ""), str(voucher or ""), str(course_session or ""), str(student or ""))
	)
	return "{0}{1}".format(EVENT_PREFIXES[event_kind], sha256(identity.encode()).hexdigest()[:24])


def makeup_parent_email_subject(context):
	student_name = context.get("student_name") or context.get("student") or _("Student")
	if context.get("event_kind") == "voucher_issued":
		return _("Your Makeup Voucher Is Ready - {0}").format(student_name)
	return _("Makeup Class Confirmed - {0}").format(student_name)


def makeup_voucher_issued_email_message(context):
	return _makeup_parent_email_card(
		context,
		heading=_("Your Makeup Voucher Is Ready"),
		intro=_(
			"A makeup voucher has been created successfully. You can view and use it in the Parent Portal."
		),
		rows=[
			(_("Student"), context.get("student_name")),
			(_("Original course"), context.get("course")),
			(_("Original class date"), context.get("date_display")),
			(_("Original class time"), _time_range(context)),
			(_("Campus"), context.get("campus")),
			(_("Voucher"), context.get("voucher_label")),
			(_("Valid until"), context.get("expiry_date_display")),
		],
		button_label=_("View Makeup Voucher"),
	)


def makeup_booking_email_message(context):
	return _makeup_parent_email_card(
		context,
		heading=_("Makeup Class Confirmed"),
		intro=_("The makeup class has been booked successfully."),
		rows=[
			(_("Student"), context.get("student_name")),
			(_("Course"), context.get("course")),
			(_("Campus"), context.get("campus")),
			(_("Date"), context.get("date_display")),
			(_("Time"), _time_range(context)),
			(_("Classroom"), context.get("classroom")),
			(_("Teacher"), context.get("teacher_name")),
			(_("Voucher"), context.get("voucher_label")),
		],
		button_label=_("View Class Schedule"),
	)


def _makeup_parent_email_message(context):
	if context.get("event_kind") == "voucher_issued":
		return makeup_voucher_issued_email_message(context)
	return makeup_booking_email_message(context)


def _makeup_parent_email_card(context, *, heading, intro, rows, button_label):
	row_html = "".join(
		"""<tr>
			<td style="padding:9px 0;color:#64748b;">{0}</td>
			<td style="padding:9px 0;text-align:right;font-weight:700;">{1}</td>
		</tr>""".format(escape_html(str(label)), escape_html(str(value)))
		for label, value in rows
		if value not in (None, "")
	)
	parent_name = context.get("parent_name")
	greeting = _("Hi {0},").format(parent_name) if parent_name else _("Hi,")
	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">{school_name}</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">{heading}</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						<table style="width:100%;border-collapse:collapse;margin:0 0 22px;">{rows}</table>
						<p style="margin:0 0 22px;">
							<a href="{portal_url}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">{button_label}</a>
						</p>
						<p style="margin:0;font-size:14px;line-height:1.5;color:#64748b;">If you have any questions, please contact {school_name}.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		school_name=escape_html(context.get("school_name") or "Queensland Art School"),
		heading=escape_html(heading),
		greeting=escape_html(greeting),
		intro=escape_html(intro),
		rows=row_html,
		portal_url=escape_html(context.get("portal_url") or _parent_portal_url("/vouchers")),
		button_label=escape_html(button_label),
	)


def _build_makeup_parent_context(
	event_kind: str,
	*,
	voucher: str,
	course_session: str | None = None,
	student: str | None = None,
):
	voucher_doc = frappe.get_doc("Makeup Voucher", voucher)
	original_student = voucher_doc.get("student")
	selected_student = (
		student
		or voucher_doc.get("used_by_student")
		or voucher_doc.get("redeemed_student")
		or original_student
	)
	student_row = _student_row(selected_student)
	owner_student_row = student_row if selected_student == original_student else _student_row(original_student)
	parent_name = (owner_student_row or {}).get("guardian") or (student_row or {}).get("guardian")
	recipient = _parent_recipient(parent_name)
	session_id = (
		voucher_doc.get("original_session")
		if event_kind == "voucher_issued"
		else course_session or voucher_doc.get("used_on_session")
	)
	session = _session_context(session_id)
	settings = get_invoice_settings()
	portal_path = (
		"/vouchers"
		if event_kind == "voucher_issued"
		else "/schedule/{0}".format(quote(str(selected_student or ""), safe=""))
	)
	return {
		"event_kind": event_kind,
		"voucher": voucher_doc.name,
		"voucher_label": get_makeup_voucher_label(voucher_doc),
		"recipient": recipient,
		"parent_name": recipient.get("parent_name"),
		"student": selected_student,
		"student_name": (student_row or {}).get("student_name") or selected_student,
		"course": session.get("course") or voucher_doc.get("course"),
		"campus": session.get("campus"),
		"classroom": session.get("classroom"),
		"teacher_name": session.get("teacher_name"),
		"date_display": session.get("date_display"),
		"start_time": session.get("start_time"),
		"end_time": session.get("end_time"),
		"expiry_date_display": (
			formatdate(voucher_doc.get("expiry_date"), "d MMMM yyyy")
			if voucher_doc.get("expiry_date")
			else ""
		),
		"portal_url": _parent_portal_url(portal_path),
		"school_name": settings.get("school_name") or "Queensland Art School",
		"school_email": (settings.get("school_email") or "").strip().lower(),
		"course_session": session_id,
	}


def _student_row(student):
	if not student:
		return {}
	fields = ["name", "student_name"]
	if frappe.db.has_column("Student", "guardian"):
		fields.append("guardian")
	return frappe.db.get_value("Student", student, fields, as_dict=True) or {}


def _parent_recipient(parent):
	if not parent:
		return {"email": "", "for_user": None, "parent": None, "parent_name": ""}
	fields = ["name"]
	for fieldname in ["parent_name", "linked_user", "email", "email_id", "contact_email"]:
		if frappe.db.has_column("Parent", fieldname):
			fields.append(fieldname)
	row = frappe.db.get_value("Parent", parent, fields, as_dict=True) or {}
	linked_user = row.get("linked_user")
	email = _first_value(row, ["email", "email_id", "contact_email"])
	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user
	return {
		"email": str(email or "").strip().lower(),
		"for_user": linked_user,
		"parent": parent,
		"parent_name": row.get("parent_name") or parent,
	}


def _session_context(course_session):
	if not course_session:
		return {}
	session = frappe.db.get_value(
		"Course Sessions",
		course_session,
		["name", "weekly_timeslot", "session_date", "teacher_override"],
		as_dict=True,
	)
	if not session:
		return {}
	timeslot = {}
	if session.get("weekly_timeslot"):
		timeslot = frappe.db.get_value(
			"Weekly Timeslot",
			session.get("weekly_timeslot"),
			["course", "campus", "classroom", "teacher", "start_time", "end_time"],
			as_dict=True,
		) or {}
	teacher = session.get("teacher_override") or timeslot.get("teacher")
	teacher_name = teacher
	if teacher:
		teacher_row = frappe.db.get_value("Teacher", teacher, ["teacher_name"], as_dict=True) or {}
		teacher_name = teacher_row.get("teacher_name") or teacher
	return {
		"course": timeslot.get("course"),
		"campus": timeslot.get("campus"),
		"classroom": timeslot.get("classroom"),
		"teacher_name": teacher_name,
		"date_display": (
			formatdate(session.get("session_date"), "d MMMM yyyy")
			if session.get("session_date")
			else ""
		),
		"start_time": _format_time(timeslot.get("start_time")),
		"end_time": _format_time(timeslot.get("end_time")),
	}


def _makeup_event_is_current(event_kind, voucher, course_session, student):
	if not voucher or not frappe.db.exists("Makeup Voucher", voucher):
		return False
	fields = ["status", "student", "used_on_session"]
	for fieldname in ["used_by_student", "redeemed_student"]:
		if frappe.db.has_column("Makeup Voucher", fieldname):
			fields.append(fieldname)
	row = frappe.db.get_value(
		"Makeup Voucher",
		voucher,
		fields,
		as_dict=True,
	)
	if not row:
		return False
	if event_kind == "voucher_issued":
		return row.get("status") in {"Valid", "Used"}
	if event_kind != "booking_confirmed":
		return False
	used_student = row.get("used_by_student") or row.get("redeemed_student") or row.get("student")
	if row.get("status") != "Used" or row.get("used_on_session") != course_session or used_student != student:
		return False
	return _makeup_booking_attendance_exists(voucher, course_session, student)


def _makeup_booking_attendance_exists(voucher, course_session, student):
	base_filters = {
		"course_session": course_session,
		"student": student,
		"status": ["!=", "Cancelled"],
	}
	if frappe.db.has_column(ATTENDANCE_DOCTYPE, "source_doctype") and frappe.db.has_column(
		ATTENDANCE_DOCTYPE, "source_document"
	):
		if frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{
				**base_filters,
				"source_doctype": "Makeup Voucher",
				"source_document": voucher,
			},
		):
			return True
	if frappe.db.has_column(ATTENDANCE_DOCTYPE, "makeup_voucher"):
		return bool(
			frappe.db.exists(
				ATTENDANCE_DOCTYPE,
				{**base_filters, "makeup_voucher": voucher},
			)
		)
	return False


def _reserve_notification_event(event_key, context, subject, message):
	if not _notification_log_available():
		return None
	lock_name = "qas-makeup-parent-email:{0}".format(event_key)
	with frappe.cache.lock(lock_name, timeout=30, blocking_timeout=10):
		if _notification_event_exists(event_key):
			raise frappe.DuplicateEntryError
		recipient = context["recipient"]
		log = frappe.new_doc("Notification Log")
		log.subject = subject
		log.type = "Alert"
		log.email_content = message
		log.document_type = "Makeup Voucher"
		log.document_name = context["voucher"]
		log.from_user = frappe.session.user
		if log.meta.has_field("for_user"):
			log.for_user = recipient.get("for_user") or frappe.session.user
		for fieldname, value in {
			"event_key": event_key,
			"email_to": recipient.get("email"),
			"recipient_email": recipient.get("email"),
			"reference_doctype": "Makeup Voucher",
			"reference_name": context["voucher"],
		}.items():
			if log.meta.has_field(fieldname):
				setattr(log, fieldname, value)
		if not log.meta.has_field("event_key"):
			log.document_name = event_key
		log.flags.ignore_permissions = True
		log.insert(ignore_permissions=True)
		return log.name


def _notification_event_exists(event_key):
	if not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	filters = {"event_key": event_key} if meta.has_field("event_key") else {"document_name": event_key}
	return bool(frappe.db.exists("Notification Log", filters))


def _notification_log_sent(notification_log):
	if not notification_log or not _notification_log_available():
		return False
	meta = frappe.get_meta("Notification Log")
	fields = [
		fieldname
		for fieldname in ["status", "delivery_status", "email_status"]
		if meta.has_field(fieldname)
	]
	if not fields:
		return False
	row = frappe.db.get_value("Notification Log", notification_log, fields, as_dict=True) or {}
	return any(row.get(fieldname) == "Sent" for fieldname in fields)


def _refresh_notification_log(notification_log, context, subject, message):
	if not notification_log or not _notification_log_available():
		return
	meta = frappe.get_meta("Notification Log")
	values = {"subject": subject, "email_content": message}
	recipient = context["recipient"]
	for fieldname, value in {
		"for_user": recipient.get("for_user"),
		"email_to": recipient.get("email"),
		"recipient_email": recipient.get("email"),
	}.items():
		if meta.has_field(fieldname) and value:
			values[fieldname] = value
	frappe.db.set_value("Notification Log", notification_log, values, update_modified=False)


def _parent_portal_url(path):
	base_url = (
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or DEFAULT_PARENT_PORTAL_URL
	)
	return "{0}/{1}".format(str(base_url).rstrip("/"), str(path or "").lstrip("/"))


def _time_range(context):
	return " - ".join(
		value for value in [context.get("start_time"), context.get("end_time")] if value
	)


def _format_time(value):
	if value in (None, ""):
		return ""
	time_value = get_time(value)
	return datetime.combine(getdate(), time_value).strftime("%I:%M %p").lstrip("0")


def _first_value(row, fields):
	for fieldname in fields:
		value = row.get(fieldname)
		if value:
			return value
	return None
