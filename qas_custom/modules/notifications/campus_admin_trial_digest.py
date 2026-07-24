from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, formatdate, get_datetime_in_timezone, now_datetime

from qas_custom.modules.billing.store_credit import (
	get_invoice_store_credit_applied,
	get_invoice_total_amount,
)
from qas_custom.services.class_attendance import get_attendance_entries
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip

BRISBANE_TIMEZONE = "Australia/Brisbane"
ACTIVE_INQUIRY_STATUSES = {"Booked", "Rescheduled"}
NON_ATTENDING_STATUSES = {"Leave", "Cancelled"}
EVENT_PREFIX = "campus_admin_next_day_trial_payment:"
PAYMENT_TOLERANCE = 0.005


def run_campus_admin_next_day_trial_digests():
	"""Queue tomorrow's Campus Admin Trial digest at 7 PM Brisbane time only."""
	now = get_datetime_in_timezone(BRISBANE_TIMEZONE)
	if now.hour != 19:
		return {"skipped": True, "reason": "Outside the 7 PM Australia/Brisbane send window."}
	return enqueue_campus_admin_next_day_trial_digests(now.date() + timedelta(days=1))


def enqueue_campus_admin_next_day_trial_digests(target_date):
	groups = get_campus_admin_next_day_trial_groups(target_date)
	result = {"target_date": str(target_date), "queued": 0, "skipped": 0, "failed": 0}
	for profile, group in groups.items():
		event_key = campus_admin_trial_digest_event_key(profile, target_date)
		if _notification_event_exists(event_key):
			result["skipped"] += 1
			continue

		subject = _digest_subject(target_date)
		message = _digest_message(
			target_date,
			group.get("trials") or [],
			admin_name=(group.get("recipient") or {}).get("display_name"),
		)
		log_name = _create_notification_log(event_key, group.get("recipient") or {}, subject, message)
		recipient = (group.get("recipient") or {}).get("email")
		if not recipient:
			_mark_notification_failed(log_name, "No Campus Admin email found.")
			result["failed"] += 1
			continue
		if not outbound_email_enabled():
			_mark_notification_failed(log_name, email_block_reason())
			result["skipped"] += 1
			continue

		_mark_notification_queued(log_name)
		frappe.enqueue(
			"qas_custom.modules.notifications.campus_admin_trial_digest.send_campus_admin_next_day_trial_digest_job",
			queue="short",
			timeout=300,
			enqueue_after_commit=True,
			profile=profile,
			target_date=str(target_date),
			notification_log=log_name,
		)
		result["queued"] += 1
	return result


def send_campus_admin_next_day_trial_digest_job(profile, target_date, notification_log=None):
	target_date = str(target_date)
	group = get_campus_admin_next_day_trial_groups(target_date, profile=profile).get(profile)
	if not group or not group.get("trials"):
		_mark_notification_failed(notification_log, "No eligible next-day Trial students remain.")
		return {"sent": False, "skipped": True, "reason": "No eligible next-day Trial students remain."}

	recipient_info = group.get("recipient") or {}
	recipient = recipient_info.get("email")
	if not recipient:
		_mark_notification_failed(notification_log, "No Campus Admin email found.")
		return {"sent": False, "skipped": True, "reason": "No Campus Admin email found."}

	try:
		mail_result = sendmail_or_skip(
			action="campus_admin_next_day_trial_payment_digest",
			recipients=[recipient],
			subject=_digest_subject(target_date),
			message=_digest_message(
				target_date,
				group.get("trials") or [],
				admin_name=recipient_info.get("display_name"),
			),
			reference_doctype="Campus Admin Profile",
			reference_name=profile,
			delayed=False,
		)
		if mail_result and mail_result.get("skipped"):
			reason = mail_result.get("reason") or email_block_reason()
			_mark_notification_failed(notification_log, reason)
			return {"sent": False, "skipped": True, "reason": reason}
		_mark_notification_sent(notification_log)
		return {"sent": True, "recipient": recipient, "trial_count": len(group.get("trials") or [])}
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS Campus Admin next-day Trial digest failed: {profile}")
		_mark_notification_failed(notification_log, "Email send failed.")
		return {"sent": False, "reason": "Email send failed."}


def get_campus_admin_next_day_trial_groups(target_date, profile=None):
	target_date = str(target_date)
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"session_date": target_date, "status": ["!=", "Cancelled"]},
		fields=["name", "weekly_timeslot"],
		order_by="name asc",
		limit_page_length=0,
	)
	if not sessions:
		return {}

	session_names = [row.get("name") for row in sessions if row.get("name")]
	timeslots = _get_timeslot_map([row.get("weekly_timeslot") for row in sessions])
	attendance = get_attendance_entries(
		session_names,
		fields=[
			"course_session",
			"student",
			"enrollment_type",
			"status",
			"source_doctype",
			"source_document",
		],
		filters={"enrollment_type": "Trial", "source_doctype": "Inquiry"},
	)
	inquiry_names = sorted({row.get("source_document") for row in attendance if row.get("source_document")})
	inquiries = _get_inquiry_map(inquiry_names)
	students = _get_student_map(
		{row.get("student") for row in attendance if row.get("student")}
		| {row.get("student") for row in inquiries.values() if row.get("student")}
	)
	invoices = _get_trial_invoice_map(
		{row.get("trial_invoice") for row in inquiries.values() if row.get("trial_invoice")}
	)
	trials = _build_eligible_trial_rows(sessions, timeslots, attendance, inquiries, students, invoices)
	if not trials:
		return {}
	recipients = _get_campus_admin_recipients(
		{row.get("campus") for row in trials if row.get("campus")}, profile=profile
	)
	return _assign_trials_to_recipients(trials, recipients)


def _build_eligible_trial_rows(sessions, timeslots, attendance_rows, inquiries, students, invoices):
	sessions_by_name = {row.get("name"): row for row in sessions if row.get("name")}
	rows = []
	seen = set()
	for attendance in attendance_rows:
		if attendance.get("enrollment_type") != "Trial" or attendance.get("source_doctype") != "Inquiry":
			continue
		if (attendance.get("status") or "").strip() in NON_ATTENDING_STATUSES:
			continue
		inquiry = inquiries.get(attendance.get("source_document"))
		if not inquiry or inquiry.get("inquiry_type") != "Trial Lesson":
			continue
		if (inquiry.get("status") or "").strip() not in ACTIVE_INQUIRY_STATUSES:
			continue
		session_name = attendance.get("course_session")
		if inquiry.get("course_session") != session_name:
			continue
		session = sessions_by_name.get(session_name)
		timeslot = timeslots.get((session or {}).get("weekly_timeslot"))
		if not session or not timeslot or not timeslot.get("campus"):
			continue
		key = (inquiry.get("name"), session_name)
		if key in seen:
			continue
		seen.add(key)
		student = inquiry.get("student") or attendance.get("student")
		payment = classify_trial_invoice_payment(invoices.get(inquiry.get("trial_invoice")))
		rows.append(
			{
				"inquiry": inquiry.get("name"),
				"student": student,
				"student_name": students.get(student) or student or _("Trial student"),
				"course_session": session_name,
				"course": timeslot.get("course") or _("Class"),
				"campus": timeslot.get("campus"),
				"start_time": _display_time(timeslot.get("start_time")),
				"end_time": _display_time(timeslot.get("end_time")),
				"payment_status": payment.get("status"),
				"outstanding_amount": payment.get("outstanding_amount"),
			}
		)
	rows.sort(key=lambda row: (row["campus"], row["start_time"], row["student_name"], row["inquiry"]))
	return rows


def classify_trial_invoice_payment(invoice):
	if not invoice or cint(invoice.get("docstatus") or 0) != 1 or invoice.get("status") == "Cancelled":
		return {"status": "Payment Required at Reception", "outstanding_amount": None}

	outstanding = max(0, flt(invoice.get("outstanding_amount") or 0))
	payable_total = max(0, flt(invoice.get("payable_total") or 0))
	if outstanding <= PAYMENT_TOLERANCE:
		return {"status": "Paid", "outstanding_amount": 0}
	if payable_total > PAYMENT_TOLERANCE and outstanding < payable_total - PAYMENT_TOLERANCE:
		return {"status": "Partially Paid", "outstanding_amount": outstanding}
	return {"status": "Unpaid", "outstanding_amount": outstanding}


def _assign_trials_to_recipients(trials, recipients):
	groups = {}
	for recipient in recipients:
		assigned = set(recipient.get("campuses") or [])
		visible = [row for row in trials if row.get("campus") in assigned]
		if visible:
			groups[recipient.get("profile")] = {"recipient": recipient, "trials": visible}
	return groups


def _get_timeslot_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", names]},
		fields=["name", "course", "campus", "start_time", "end_time"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _get_inquiry_map(names):
	if not names:
		return {}
	rows = frappe.get_all(
		"Inquiry",
		filters={"name": ["in", names]},
		fields=["name", "inquiry_type", "status", "course_session", "student", "trial_invoice"],
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows}


def _get_student_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Student",
		filters={"name": ["in", names]},
		fields=["name", "student_name"],
		limit_page_length=0,
	)
	return {row.get("name"): row.get("student_name") or row.get("name") for row in rows}


def _get_trial_invoice_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	fields = ["name", "docstatus", "status", "grand_total", "rounded_total", "outstanding_amount"]
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", names]},
		fields=fields,
		limit_page_length=0,
	)
	result = {}
	for row in rows:
		total = get_invoice_total_amount(row)
		applied_credit = get_invoice_store_credit_applied(row.get("name"))
		row["payable_total"] = max(0, flt(total) - flt(applied_credit))
		result[row.get("name")] = row
	return result


def _get_campus_admin_recipients(campuses, profile=None):
	campuses = sorted({campus for campus in campuses if campus})
	if not campuses or not frappe.db.exists("DocType", "Campus Admin Profile"):
		return []
	child_filters = {
		"campus": ["in", campuses],
		"parenttype": "Campus Admin Profile",
	}
	if profile:
		child_filters["parent"] = profile
	assignments = frappe.get_all(
		"Campus Admin Profile Campus",
		filters=child_filters,
		fields=["parent", "campus"],
		limit_page_length=0,
	)
	profile_names = sorted({row.get("parent") for row in assignments if row.get("parent")})
	if not profile_names:
		return []
	profiles = frappe.get_all(
		"Campus Admin Profile",
		filters={"name": ["in", profile_names], "active": 1},
		fields=["name", "user"],
		limit_page_length=0,
	)
	user_names = sorted({row.get("user") for row in profiles if row.get("user")})
	users = (
		frappe.get_all(
			"User",
			filters={"name": ["in", user_names], "enabled": 1},
			fields=["name", "email", "full_name"],
			limit_page_length=0,
		)
		if user_names
		else []
	)
	users_by_name = {row.get("name"): row for row in users}
	campuses_by_profile = defaultdict(set)
	for row in assignments:
		campuses_by_profile[row.get("parent")].add(row.get("campus"))

	recipients = []
	for profile_row in profiles:
		user = users_by_name.get(profile_row.get("user"))
		email = _normalise_email((user or {}).get("email") or (user or {}).get("name"))
		if not email:
			continue
		recipients.append(
			{
				"profile": profile_row.get("name"),
				"user": profile_row.get("user"),
				"email": email,
				"display_name": (user or {}).get("full_name") or email,
				"campuses": sorted(campuses_by_profile.get(profile_row.get("name")) or []),
			}
		)
	return recipients


def campus_admin_trial_digest_event_key(profile, target_date):
	identity = "\x1f".join((str(profile or ""), str(target_date or "")))
	return f"{EVENT_PREFIX}{sha256(identity.encode()).hexdigest()[:20]}"


def _digest_subject(target_date):
	return _("Tomorrow's Trial students — {0}").format(formatdate(target_date, "EEEE d MMMM yyyy"))


def _digest_message(target_date, trials, admin_name=None):
	date_display = escape_html(formatdate(target_date, "EEEE d MMMM yyyy"))
	admin_name = str(admin_name or "").strip()
	greeting = _("Hi {0},").format(escape_html(admin_name)) if admin_name else _("Hello,")
	trial_count = len(trials)
	summary = (
		_("There is {0} Trial student tomorrow.").format(trial_count)
		if trial_count == 1
		else _("There are {0} Trial students tomorrow.").format(trial_count)
	)
	sections = []
	by_campus = defaultdict(list)
	for trial in trials:
		by_campus[trial.get("campus") or _("Not assigned")].append(trial)
	for campus in sorted(by_campus):
		cards = "".join(_trial_card(trial) for trial in by_campus[campus])
		sections.append(
			f"""
			<tr>
				<td style="padding:8px 26px 6px;color:#172033;font-size:19px;font-weight:700;line-height:1.35;">{escape_html(str(campus))}</td>
			</tr>
			<tr>
				<td style="padding:0 26px 12px;">
					<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;">{cards}</table>
				</td>
			</tr>
			"""
		)

	return """
		<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#172033;">
			<tr>
				<td align="center" style="padding:24px 12px;">
					<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:680px;border-collapse:separate;background-color:#ffffff;border-radius:16px;overflow:hidden;">
						<tr><td style="height:6px;background-color:#ef6a4c;font-size:0;line-height:0;">&nbsp;</td></tr>
						<tr>
							<td style="padding:24px 26px;background-color:#172033;">
								<div style="margin:0 0 6px;color:#f7b6a4;font-size:12px;font-weight:700;letter-spacing:1px;line-height:1.3;text-transform:uppercase;">{school_name}</div>
								<div style="margin:0 0 6px;color:#ffffff;font-size:26px;font-weight:700;line-height:1.25;">{title}</div>
								<div style="color:#dbe4f0;font-size:15px;line-height:1.4;">{date_display}</div>
							</td>
						</tr>
						<tr>
							<td style="padding:24px 26px 12px;">
								<div style="margin:0 0 8px;color:#172033;font-size:18px;font-weight:700;line-height:1.4;">{greeting}</div>
								<div style="margin:0;color:#64748b;font-size:15px;line-height:1.55;">{summary}</div>
							</td>
						</tr>
						{sections}
						<tr>
							<td style="padding:16px 26px 24px;border-top:1px solid #e2e8f0;color:#64748b;font-size:13px;line-height:1.5;text-align:center;">
								{reception_note}
							</td>
						</tr>
					</table>
				</td>
			</tr>
		</table>
	""".format(
		school_name=escape_html(_("Queensland Art School")),
		title=escape_html(_("Tomorrow's Trial Students")),
		date_display=date_display,
		greeting=greeting,
		summary=escape_html(summary),
		sections="".join(sections),
		reception_note=escape_html(
			_("Please collect payment at reception when a student is marked Payment Required at Reception.")
		),
	)


def _trial_card(trial):
	status = trial.get("payment_status") or "Payment Required at Reception"
	colors = {
		"Paid": ("#dcfce7", "#166534"),
		"Partially Paid": ("#fef3c7", "#92400e"),
		"Unpaid": ("#fee2e2", "#991b1b"),
		"Payment Required at Reception": ("#fee2e2", "#991b1b"),
	}
	background, foreground = colors.get(status, colors["Payment Required at Reception"])
	amount = ""
	if status in {"Partially Paid", "Unpaid"}:
		amount = f" · {fmt_money(flt(trial.get('outstanding_amount') or 0), currency='AUD')}"
	return """
		<tr>
			<td style="padding:0 0 12px;">
				<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;border:1px solid #e2e8f0;border-radius:12px;background-color:#ffffff;">
					<tr>
						<td style="padding:16px 18px;">
							<div style="margin:0 0 5px;color:#172033;font-size:18px;font-weight:700;line-height:1.35;">{student_name}</div>
							<div style="margin:0 0 5px;color:#334155;font-size:15px;line-height:1.4;">{start_time} &ndash; {end_time} · {course}</div>
							<div style="margin:10px 0 0;">
								<span style="display:inline-block;padding:6px 10px;border-radius:999px;background-color:{background};color:{foreground};font-size:12px;font-weight:700;line-height:1.2;">{status}{amount}</span>
							</div>
						</td>
					</tr>
				</table>
			</td>
		</tr>
	""".format(
		student_name=escape_html(str(trial.get("student_name") or _("Trial student"))),
		start_time=escape_html(str(trial.get("start_time") or "-")),
		end_time=escape_html(str(trial.get("end_time") or "-")),
		course=escape_html(str(trial.get("course") or _("Class"))),
		background=background,
		foreground=foreground,
		status=escape_html(_(status)),
		amount=escape_html(amount),
	)


def _display_time(value):
	text = str(value or "").strip()
	return text[:5] if len(text) >= 5 else text or "-"


def _normalise_email(value):
	return str(value or "").strip().lower()


def _notification_event_exists(event_key):
	if not frappe.db.exists("DocType", "Notification Log"):
		return False
	meta = frappe.get_meta("Notification Log")
	if meta.has_field("event_key"):
		return bool(frappe.db.exists("Notification Log", {"event_key": event_key}))
	return bool(frappe.db.exists("Notification Log", {"document_name": event_key}))


def _create_notification_log(event_key, recipient, subject, message):
	if not frappe.db.exists("DocType", "Notification Log"):
		return None
	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.type = "Alert"
	log.email_content = message
	log.document_type = "Campus Admin Profile"
	log.document_name = recipient.get("profile")
	log.from_user = frappe.session.user
	if log.meta.has_field("for_user"):
		log.for_user = recipient.get("user") or frappe.session.user
	if not log.meta.has_field("event_key"):
		log.document_name = event_key
	for fieldname, value in {
		"event_key": event_key,
		"email_to": recipient.get("email"),
		"recipient_email": recipient.get("email"),
	}.items():
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
	return {
		fieldname: status
		for fieldname in ["status", "delivery_status", "email_status"]
		if meta.has_field(fieldname)
	}
