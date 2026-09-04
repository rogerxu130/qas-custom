from __future__ import annotations

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, escape_html, get_url, getdate, now_datetime, strip_html, validate_email_address
from frappe.utils.file_manager import save_file

from qas_custom.services.announcements import (
	_get_announcement_image_upload,
	_message_html,
	_normalise_announcement_image_filename,
	_read_announcement_image,
	_validate_announcement_image_content,
)
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled


ADMIN_ROLES = {"School Admin", "System Manager"}
PARENT_EMAIL_DOCTYPE = "School Parent Email"
PARENT_EMAIL_RECIPIENT_DOCTYPE = "School Parent Email Recipient"
NON_ATTENDING_STATUSES = {"Cancelled", "Leave"}
AUDIENCE_MODES = {"Term", "Dated Course Sessions", "Dated Teacher Sessions", "Specific Parent"}


def get_school_admin_parent_emails_data(status=None, limit=100):
	_require_school_admin()
	filters = {"status": status} if status else {}
	rows = frappe.get_all(
		PARENT_EMAIL_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"subject",
			"status",
			"audience_mode",
			"term",
			"session_date",
			"teacher",
			"specific_parent",
			"course_sessions_json",
			"recipient_count",
			"selected_count",
			"sent_count",
			"skipped_count",
			"failed_count",
			"queued_at",
			"completed_at",
			"modified",
		],
		order_by="modified desc",
		limit=_limit(limit, 100, 200),
	)
	return {"items": [_summary_payload(row) for row in rows]}


def get_school_admin_parent_email_data(parent_email=None):
	_require_school_admin()
	doc = _get_parent_email(parent_email)
	payload = _doc_payload(doc)
	payload["recipients"] = _recipient_payloads(doc.name)
	return payload


def save_school_admin_parent_email_data(parent_email=None, payload=None):
	_require_school_admin()
	data = _parse_payload(payload)
	if parent_email:
		doc = _get_parent_email(parent_email)
		if doc.status != "Draft":
			frappe.throw(_("A Parent Email cannot be edited after sending starts."))
	else:
		doc = frappe.new_doc(PARENT_EMAIL_DOCTYPE)
		doc.status = "Draft"
		doc.created_by = frappe.session.user

	previous_audience = _audience_state(doc)
	_apply_draft_payload(doc, data)
	if _audience_state(doc) != previous_audience:
		_clear_preview(doc)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return get_school_admin_parent_email_data(doc.name)


def upload_school_admin_parent_email_inline_image_data(parent_email=None):
	_require_school_admin()
	doc = _get_parent_email(parent_email)
	if doc.status != "Draft":
		frappe.throw(_("Images can only be uploaded to a Parent Email Draft."))
	upload = _get_announcement_image_upload()
	content = _read_announcement_image(upload)
	image_format = _validate_announcement_image_content(content)
	file_doc = save_file(
		_normalise_announcement_image_filename(upload, image_format),
		content,
		PARENT_EMAIL_DOCTYPE,
		doc.name,
		is_private=0,
		df="inline_image",
	)
	frappe.db.commit()
	return {"image_url": get_url(file_doc.file_url), "file_name": file_doc.file_name}


def get_school_admin_parent_email_audience_options_data(session_date=None):
	_require_school_admin()
	if not session_date:
		return {"sessions": [], "teachers": []}
	session_date = getdate(session_date)
	sessions = _sessions_on_date(session_date)
	counts = _session_attendance_counts([row.get("name") for row in sessions])
	timeslots = _timeslot_map([row.get("weekly_timeslot") for row in sessions])
	teacher_names = {
		_effective_teacher(row, timeslots.get(row.get("weekly_timeslot")))
		for row in sessions
		if counts.get(row.get("name"), 0)
	}
	teachers = _teacher_map(teacher_names)
	items = []
	for session in sessions:
		timeslot = timeslots.get(session.get("weekly_timeslot")) or {}
		teacher = _effective_teacher(session, timeslot)
		student_count = counts.get(session.get("name"), 0)
		items.append(
			{
				"value": session.get("name"),
				"label": _session_label(session, timeslot, teachers.get(teacher)),
				"teacher": teacher,
				"teacher_label": teachers.get(teacher) or teacher or "",
				"student_count": student_count,
				"has_students": bool(student_count),
			}
		)
	return {
		"sessions": items,
		"teachers": [
			{"value": name, "label": label}
			for name, label in sorted(teachers.items(), key=lambda item: item[1].casefold())
			if name in teacher_names
		],
	}


def preview_school_admin_parent_email_recipients_data(parent_email=None):
	_require_school_admin()
	doc = _get_parent_email(parent_email)
	if doc.status != "Draft":
		frappe.throw(_("Recipients can only be previewed for a Parent Email Draft."))
	_validate_audience(doc)
	rows = _resolve_recipients(doc)
	if not rows:
		frappe.throw(_("No Parents matched this email audience."))
	doc.preview_json = json.dumps(rows, ensure_ascii=False, default=str)
	doc.preview_token = frappe.generate_hash(length=24)
	doc.previewed_at = now_datetime()
	doc.recipient_count = len(rows)
	doc.selected_count = len([row for row in rows if row.get("eligible")])
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"items": rows, "preview_token": doc.preview_token}


def send_school_admin_parent_email_data(parent_email=None, preview_token=None, selected_parents=None):
	_require_school_admin()
	doc = _get_parent_email(parent_email)
	if doc.status != "Draft":
		return get_school_admin_parent_email_data(doc.name)
	if not preview_token or preview_token != doc.preview_token:
		frappe.throw(_("The recipient preview is stale. Preview recipients again."))
	if not strip_html(doc.subject or "").strip():
		frappe.throw(_("Email subject is required."))
	if not strip_html(doc.body or "").strip():
		frappe.throw(_("Email body is required."))

	selected = set(_parse_string_list(selected_parents))
	preview_rows = _load_json_list(doc.preview_json)
	reviewed_parents = {row.get("parent") for row in preview_rows if row.get("parent")}
	if not selected or not selected.issubset(reviewed_parents):
		frappe.throw(_("Select at least one Parent from the current recipient preview."))

	current_rows = {row.get("parent"): row for row in _resolve_recipients(doc)}
	frappe.db.delete(PARENT_EMAIL_RECIPIENT_DOCTYPE, {"parent_email": doc.name})
	queued_rows = []
	for preview in preview_rows:
		parent = preview.get("parent")
		current = current_rows.get(parent)
		row = frappe.new_doc(PARENT_EMAIL_RECIPIENT_DOCTYPE)
		row.parent_email = doc.name
		row.parent = parent
		row.parent_name = (current or preview).get("parent_name") or parent
		row.email = (current or preview).get("email") or ""
		row.students_json = json.dumps((current or preview).get("students") or [], ensure_ascii=False)
		row.student_ids_json = json.dumps((current or preview).get("student_ids") or [], ensure_ascii=False)
		row.match_reasons_json = json.dumps((current or preview).get("match_reasons") or [], ensure_ascii=False)
		row.selected = 1 if parent in selected else 0
		row.eligible = 1 if current and current.get("eligible") else 0
		if parent not in selected:
			row.status = "Skipped"
			row.error = "Deselected by School Admin."
		elif not current:
			row.status = "Skipped"
			row.error = "Parent no longer matches the reviewed audience."
		elif not current.get("eligible"):
			row.status = "Skipped"
			row.error = current.get("reason") or "Parent is not eligible for mass email."
		elif not outbound_email_enabled():
			row.status = "Skipped"
			row.error = email_block_reason()
		else:
			row.status = "Queued"
			queued_rows.append(row)
		row.insert(ignore_permissions=True)

	doc.status = "Queued" if queued_rows else "Failed"
	doc.queued_at = now_datetime()
	doc.selected_count = len(selected)
	doc.recipient_count = len(preview_rows)
	doc.save(ignore_permissions=True)
	_refresh_delivery_totals(doc.name, commit=False)
	for row in queued_rows:
		frappe.enqueue(
			"qas_custom.services.parent_emails.send_parent_email_recipient_job",
			queue="short",
			timeout=180,
			enqueue_after_commit=True,
			recipient=row.name,
		)
	frappe.db.commit()
	return get_school_admin_parent_email_data(doc.name)


def retry_school_admin_parent_email_failures_data(parent_email=None):
	_require_school_admin()
	doc = _get_parent_email(parent_email)
	if doc.status not in {"Failed", "Partially Failed"}:
		frappe.throw(_("Only failed Parent Email recipients can be retried."))
	if not outbound_email_enabled():
		frappe.throw(_(email_block_reason()))
	rows = frappe.get_all(
		PARENT_EMAIL_RECIPIENT_DOCTYPE,
		filters={"parent_email": doc.name, "status": "Failed", "selected": 1},
		fields=["name", "parent"],
		limit=0,
	)
	if not rows:
		frappe.throw(_("This Parent Email has no failed recipients to retry."))
	for row in rows:
		current = _parent_recipient(row.get("parent"), students=[], student_ids=[], match_reasons=[])
		if not current.get("eligible"):
			frappe.db.set_value(
				PARENT_EMAIL_RECIPIENT_DOCTYPE,
				row.get("name"),
				{"status": "Skipped", "error": current.get("reason") or "Parent is not eligible for mass email."},
				update_modified=True,
			)
			continue
		frappe.db.set_value(
			PARENT_EMAIL_RECIPIENT_DOCTYPE,
			row.get("name"),
			{"email": current.get("email"), "status": "Queued", "error": ""},
			update_modified=True,
		)
		frappe.enqueue(
			"qas_custom.services.parent_emails.send_parent_email_recipient_job",
			queue="short",
			timeout=180,
			enqueue_after_commit=True,
			recipient=row.get("name"),
		)
	doc.status = "Queued"
	doc.completed_at = None
	doc.save(ignore_permissions=True)
	_refresh_delivery_totals(doc.name, commit=False)
	frappe.db.commit()
	return get_school_admin_parent_email_data(doc.name)


def send_parent_email_recipient_job(recipient: str):
	if not recipient or not frappe.db.exists(PARENT_EMAIL_RECIPIENT_DOCTYPE, recipient):
		return {"sent": False, "reason": "Recipient was not found."}
	row = frappe.get_doc(PARENT_EMAIL_RECIPIENT_DOCTYPE, recipient)
	if row.status != "Queued":
		return {"sent": row.status == "Sent", "reason": "Recipient is not queued."}
	doc = frappe.get_doc(PARENT_EMAIL_DOCTYPE, row.parent_email)
	row.attempts = cint(row.attempts) + 1
	row.last_attempt_at = now_datetime()
	try:
		current = _parent_recipient(row.parent, students=[], student_ids=[], match_reasons=[])
		if not current.get("eligible"):
			row.status = "Skipped"
			row.error = current.get("reason") or "Parent is not eligible for mass email."
		elif not outbound_email_enabled():
			row.status = "Skipped"
			row.error = email_block_reason()
		else:
			row.email = current.get("email")
			frappe.sendmail(
				recipients=[row.email],
				subject=doc.subject,
				message=_parent_email_message(doc),
				reference_doctype=PARENT_EMAIL_DOCTYPE,
				reference_name=doc.name,
				expose_recipients="header",
				now=True,
			)
			row.status = "Sent"
			row.sent_at = now_datetime()
			row.error = ""
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS parent email failed: {doc.name} / {row.name}")
		row.status = "Failed"
		row.error = "Email send failed."
	row.save(ignore_permissions=True)
	_refresh_delivery_totals(doc.name, commit=False)
	frappe.db.commit()
	return {"sent": row.status == "Sent", "status": row.status}


def _apply_draft_payload(doc, payload):
	for fieldname in ["subject", "audience_mode", "term", "session_date", "teacher", "specific_parent"]:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname) or "")
	if "course_sessions" in payload:
		doc.course_sessions_json = json.dumps(_parse_string_list(payload.get("course_sessions")), ensure_ascii=False)
	if "body" in payload:
		doc.body = _message_html(payload.get("body"), allowed_image_urls=_inline_image_urls(doc.name))
	if doc.audience_mode not in AUDIENCE_MODES:
		frappe.throw(_("Choose a valid Parent Email audience."))


def _validate_audience(doc):
	if doc.audience_mode == "Term" and not doc.term:
		frappe.throw(_("Term is required."))
	if doc.audience_mode == "Dated Course Sessions":
		if not doc.session_date or not _course_session_names(doc):
			frappe.throw(_("Choose a date and at least one Course Session."))
	if doc.audience_mode == "Dated Teacher Sessions" and (not doc.session_date or not doc.teacher):
		frappe.throw(_("Choose a date and Teacher."))
	if doc.audience_mode == "Specific Parent" and not doc.specific_parent:
		frappe.throw(_("Choose one Parent."))


def _resolve_recipients(doc):
	if doc.audience_mode == "Term":
		return _term_recipients(doc.term)
	if doc.audience_mode == "Dated Course Sessions":
		return _session_recipients(_validated_selected_sessions(doc))
	if doc.audience_mode == "Dated Teacher Sessions":
		return _teacher_date_recipients(doc.session_date, doc.teacher)
	if doc.audience_mode == "Specific Parent":
		students = frappe.get_all(
			"Student",
			filters={"guardian": doc.specific_parent},
			fields=["name", "student_name"],
			limit=0,
		)
		return [
			_parent_recipient(
				doc.specific_parent,
				students=[row.get("student_name") or row.get("name") for row in students],
				student_ids=[row.get("name") for row in students],
				match_reasons=["Specific Parent"],
			)
		]
	return []


def _term_recipients(term):
	rows = frappe.get_all(
		"Enrollment",
		filters={"term": term, "status": ["in", ["Planned", "Active"]]},
		fields=_safe_fields("Enrollment", ["name", "student", "parent", "term", "course"]),
		limit=0,
	)
	student_map = _student_map([row.get("student") for row in rows])
	grouped = defaultdict(lambda: {"students": [], "student_ids": [], "match_reasons": []})
	for row in rows:
		student = student_map.get(row.get("student")) or {}
		parent = row.get("parent") or student.get("parent")
		if not parent:
			continue
		_group_match(
			grouped[parent],
			student.get("label") or row.get("student"),
			row.get("student"),
			f"Term {term}",
		)
	return _finalise_grouped(grouped)


def _validated_selected_sessions(doc):
	names = _course_session_names(doc)
	rows = frappe.get_all(
		"Course Sessions",
		filters={"name": ["in", names]},
		fields=["name", "session_date", "status"],
		limit=0,
	)
	valid = {
		row.get("name")
		for row in rows
		if getdate(row.get("session_date")) == getdate(doc.session_date) and row.get("status") != "Cancelled"
	}
	if valid != set(names):
		frappe.throw(_("One or more selected Course Sessions no longer match this date."))
	return names


def _teacher_date_recipients(session_date, teacher):
	sessions = _sessions_on_date(getdate(session_date))
	timeslots = _timeslot_map([row.get("weekly_timeslot") for row in sessions])
	matching = [
		row.get("name")
		for row in sessions
		if _effective_teacher(row, timeslots.get(row.get("weekly_timeslot"))) == teacher
	]
	return _session_recipients(matching, teacher=teacher)


def _session_recipients(session_names, teacher=None):
	if not session_names:
		return []
	attendance = frappe.get_all(
		"Class Attendance Entry",
		filters={
			"course_session": ["in", session_names],
			"status": ["not in", sorted(NON_ATTENDING_STATUSES)],
		},
		fields=["name", "course_session", "student", "enrollment_type", "status"],
		limit=0,
	)
	student_map = _student_map([row.get("student") for row in attendance])
	grouped = defaultdict(lambda: {"students": [], "student_ids": [], "match_reasons": []})
	for row in attendance:
		student = student_map.get(row.get("student")) or {}
		parent = student.get("parent")
		if not parent:
			continue
		reason = f"{row.get('course_session')} ({row.get('enrollment_type') or 'Session roster'})"
		if teacher:
			reason = f"{teacher}: {reason}"
		_group_match(grouped[parent], student.get("label") or row.get("student"), row.get("student"), reason)
	return _finalise_grouped(grouped)


def _group_match(group, student_label, student_id, reason):
	if student_label and student_label not in group["students"]:
		group["students"].append(student_label)
	if student_id and student_id not in group["student_ids"]:
		group["student_ids"].append(student_id)
	if reason and reason not in group["match_reasons"]:
		group["match_reasons"].append(reason)


def _finalise_grouped(grouped):
	return [
		_parent_recipient(parent, **values)
		for parent, values in sorted(grouped.items(), key=lambda item: item[0].casefold())
	]


def _parent_recipient(parent, *, students, student_ids, match_reasons):
	fields = _safe_fields(
		"Parent",
		["name", "parent_name", "email", "email_id", "contact_email", "linked_user", "status", "mass_email_unsubscribed"],
	)
	row = frappe.db.get_value("Parent", parent, fields, as_dict=True) if parent else None
	if not row:
		return {
			"parent": parent,
			"parent_name": parent,
			"email": "",
			"students": students,
			"student_ids": student_ids,
			"match_reasons": match_reasons,
			"eligible": False,
			"selected": False,
			"reason": "Parent record was not found.",
		}
	email = next((str(row.get(field) or "").strip() for field in ["email", "email_id", "contact_email", "linked_user"] if row.get(field)), "")
	reason = ""
	if row.get("status") == "Inactive":
		reason = "Parent is inactive."
	elif cint(row.get("mass_email_unsubscribed")):
		reason = "Parent unsubscribed from mass emails."
	elif not email or not validate_email_address(email, throw=False):
		reason = "No valid parent email found."
	return {
		"parent": row.get("name"),
		"parent_name": row.get("parent_name") or row.get("name"),
		"email": email,
		"students": students,
		"student_ids": student_ids,
		"match_reasons": match_reasons,
		"eligible": not reason,
		"selected": not reason,
		"reason": reason,
	}


def _student_map(student_names):
	names = sorted({name for name in student_names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Student",
		filters={"name": ["in", names]},
		fields=_safe_fields("Student", ["name", "student_name", "first_name", "last_name", "guardian", "parent"]),
		limit=0,
	)
	return {
		row.get("name"): {
			"parent": row.get("guardian") or row.get("parent"),
			"label": row.get("student_name") or " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or row.get("name"),
		}
		for row in rows
	}


def _sessions_on_date(session_date):
	return frappe.get_all(
		"Course Sessions",
		filters={"session_date": session_date, "status": ["!=", "Cancelled"]},
		fields=_safe_fields(
			"Course Sessions",
			["name", "weekly_timeslot", "session_date", "status", "teacher", "teacher_override", "course", "campus", "start_time"],
		),
		order_by="start_time asc, name asc",
		limit=0,
	)


def _session_attendance_counts(session_names):
	if not session_names:
		return {}
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters={"course_session": ["in", session_names], "status": ["not in", sorted(NON_ATTENDING_STATUSES)]},
		fields=["course_session", "count(name) as student_count"],
		group_by="course_session",
		limit_page_length=0,
	)
	return {row.get("course_session"): cint(row.get("student_count")) for row in rows}


def _timeslot_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"name": ["in", names]},
		fields=_safe_fields("Weekly Timeslot", ["name", "teacher", "course", "campus", "start_time"]),
		limit=0,
	)
	return {row.get("name"): row for row in rows}


def _teacher_map(names):
	names = sorted({name for name in names if name})
	if not names:
		return {}
	rows = frappe.get_all(
		"Teacher",
		filters={"name": ["in", names]},
		fields=_safe_fields("Teacher", ["name", "teacher_name"]),
		limit=0,
	)
	return {row.get("name"): row.get("teacher_name") or row.get("name") for row in rows}


def _effective_teacher(session, timeslot=None):
	return session.get("teacher_override") or session.get("teacher") or (timeslot or {}).get("teacher")


def _session_label(session, timeslot, teacher_label):
	course = session.get("course") or timeslot.get("course") or "Course"
	campus = session.get("campus") or timeslot.get("campus") or ""
	start_time = str(session.get("start_time") or timeslot.get("start_time") or "")[:5]
	return " · ".join(filter(None, [course, campus, start_time, teacher_label, session.get("name")]))


def _refresh_delivery_totals(parent_email, *, commit=True):
	rows = frappe.get_all(
		PARENT_EMAIL_RECIPIENT_DOCTYPE,
		filters={"parent_email": parent_email},
		fields=["status", "count(name) as total"],
		group_by="status",
		limit_page_length=0,
	)
	counts = {row.get("status"): cint(row.get("total")) for row in rows}
	queued = counts.get("Queued", 0)
	sent = counts.get("Sent", 0)
	skipped = counts.get("Skipped", 0)
	failed = counts.get("Failed", 0)
	values = {
		"queued_count": queued,
		"sent_count": sent,
		"skipped_count": skipped,
		"failed_count": failed,
	}
	if queued:
		values["status"] = "Sending"
		values["completed_at"] = None
	elif failed and sent:
		values["status"] = "Partially Failed"
		values["completed_at"] = now_datetime()
	elif failed or not sent:
		values["status"] = "Failed"
		values["completed_at"] = now_datetime()
	else:
		values["status"] = "Completed"
		values["completed_at"] = now_datetime()
	frappe.db.set_value(PARENT_EMAIL_DOCTYPE, parent_email, values, update_modified=True)
	if commit:
		frappe.db.commit()


def _parent_email_message(doc):
	body = _message_html(doc.body or "", allowed_image_urls=_inline_image_urls(doc.name))
	body = body.replace("<img ", '<img style="display:block;max-width:100%;height:auto;border-radius:8px;" ')
	return f"""
		<div style="font-family:Arial,sans-serif;color:#1a2b4a;line-height:1.55;">
			<h2>{escape_html(doc.subject)}</h2>
			<div>{body}</div>
		</div>
	"""


def _inline_image_urls(parent_email):
	if not parent_email:
		return set()
	rows = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": PARENT_EMAIL_DOCTYPE,
			"attached_to_name": parent_email,
			"attached_to_field": "inline_image",
			"is_private": 0,
		},
		fields=["file_url"],
		limit=0,
	)
	return {get_url(row.get("file_url")) for row in rows if str(row.get("file_url") or "").startswith("/files/")}


def _recipient_payloads(parent_email):
	rows = frappe.get_all(
		PARENT_EMAIL_RECIPIENT_DOCTYPE,
		filters={"parent_email": parent_email},
		fields=[
			"name", "parent", "parent_name", "email", "students_json", "student_ids_json", "match_reasons_json",
			"selected", "eligible", "status", "sent_at", "error", "attempts", "last_attempt_at",
		],
		order_by="creation asc",
		limit=0,
	)
	return [
		{
			**dict(row),
			"students": _load_json_list(row.get("students_json")),
			"student_ids": _load_json_list(row.get("student_ids_json")),
			"match_reasons": _load_json_list(row.get("match_reasons_json")),
			"selected": bool(cint(row.get("selected"))),
			"eligible": bool(cint(row.get("eligible"))),
			"reason": row.get("error") or "",
		}
		for row in rows
	]


def _doc_payload(doc):
	payload = _summary_payload(doc)
	payload.update(
		{
			"body": doc.body or "",
			"preview_token": doc.preview_token or "",
			"previewed_at": doc.previewed_at,
			"preview_rows": _load_json_list(doc.preview_json),
			"queued_count": cint(doc.queued_count),
			"skipped_count": cint(doc.skipped_count),
			"failed_count": cint(doc.failed_count),
		}
	)
	return payload


def _summary_payload(row):
	return {
		"name": row.get("name"),
		"subject": row.get("subject") or "",
		"status": row.get("status") or "Draft",
		"audience_mode": row.get("audience_mode") or "Term",
		"term": row.get("term") or "",
		"session_date": row.get("session_date"),
		"teacher": row.get("teacher") or "",
		"specific_parent": row.get("specific_parent") or "",
		"course_sessions": _load_json_list(row.get("course_sessions_json")),
		"recipient_count": cint(row.get("recipient_count")),
		"selected_count": cint(row.get("selected_count")),
		"sent_count": cint(row.get("sent_count")),
		"skipped_count": cint(row.get("skipped_count")),
		"failed_count": cint(row.get("failed_count")),
		"queued_at": row.get("queued_at"),
		"completed_at": row.get("completed_at"),
		"modified": row.get("modified"),
	}


def _course_session_names(doc):
	return _load_json_list(doc.course_sessions_json)


def _audience_state(doc):
	return (
		doc.get("audience_mode") or "",
		doc.get("term") or "",
		str(doc.get("session_date") or ""),
		doc.get("teacher") or "",
		doc.get("specific_parent") or "",
		tuple(sorted(_course_session_names(doc))),
	)


def _clear_preview(doc):
	doc.preview_json = ""
	doc.preview_token = ""
	doc.previewed_at = None
	doc.recipient_count = 0
	doc.selected_count = 0


def _get_parent_email(name):
	if not name:
		frappe.throw(_("Parent Email is required."))
	return frappe.get_doc(PARENT_EMAIL_DOCTYPE, name)


def _parse_payload(payload):
	if not payload:
		return {}
	if isinstance(payload, dict):
		return payload
	try:
		return json.loads(payload)
	except (TypeError, ValueError):
		frappe.throw(_("Invalid Parent Email payload."))


def _parse_string_list(value):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			value = [value]
	return list(dict.fromkeys(str(item).strip() for item in (value or []) if str(item).strip()))


def _load_json_list(value):
	if isinstance(value, list):
		return value
	if not value:
		return []
	try:
		parsed = json.loads(value)
		return parsed if isinstance(parsed, list) else []
	except (TypeError, ValueError):
		return []


def _safe_fields(doctype, candidates):
	meta = frappe.get_meta(doctype)
	return [field for field in candidates if field == "name" or meta.has_field(field)]


def _limit(value, default, maximum):
	try:
		return max(1, min(cint(value or default), maximum))
	except (TypeError, ValueError):
		return default


def _require_school_admin():
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)
