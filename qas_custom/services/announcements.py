from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, escape_html, get_datetime, now_datetime

from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip
from qas_custom.services.support_view import get_support_view_parent


ADMIN_ROLES = {"School Admin", "System Manager"}
ANNOUNCEMENT_DOCTYPE = "School Announcement"
RECIPIENT_DOCTYPE = "School Announcement Recipient"
DEFAULT_PARENT_PORTAL_URL = "https://portal.queenslandartschool.com"
ANNOUNCEMENT_BCC_BATCH_SIZE = 50


def get_school_admin_announcements_data(status=None, limit=80):
	_require_school_admin()
	if not _announcement_available():
		return {"items": []}

	filters = {}
	if status:
		filters["status"] = status

	rows = frappe.get_all(
		ANNOUNCEMENT_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"title",
			"status",
			"audience_type",
			"term",
			"course",
			"course_session",
			"student",
			"publish_at",
			"expires_at",
			"send_email_on_publish",
			"recipient_count",
			"email_queued_count",
			"published_at",
			"modified",
		],
		order_by="modified desc",
		limit=_limit(limit, default=80, max_value=200),
	)
	return {"items": [_normalise_row(row) for row in rows]}


def search_school_admin_announcement_students_data(query=None, limit=20):
	_require_school_admin()
	query = str(query or "").strip()
	if len(query) < 2:
		frappe.throw(_("Search query must be at least 2 characters."))

	fields = _student_search_fields()
	search_fields = [field for field in ["name", "student_name", "first_name", "last_name", "student_code"] if field in fields]
	rows_by_name = {}
	for operator, value in [("=", query), ("like", f"{query}%"), ("like", f"%{query}%")]:
		or_filters = [["Student", field, operator, value] for field in search_fields]
		for row in frappe.get_all("Student", or_filters=or_filters, fields=fields, limit=50):
			rows_by_name.setdefault(row.name, row)
	rows = sorted(rows_by_name.values(), key=lambda row: _student_search_rank(row, query))[:_limit(limit, default=20, max_value=50)]
	return {"items": [_announcement_student_preview(row) for row in rows]}


def get_school_admin_announcement_data(announcement=None):
	_require_school_admin()
	if not announcement:
		frappe.throw(_("Announcement is required."))
	doc = frappe.get_doc(ANNOUNCEMENT_DOCTYPE, announcement)
	payload = _doc_payload(doc)
	payload["recipients"] = frappe.get_all(
		RECIPIENT_DOCTYPE,
		filters={"announcement": doc.name},
		fields=["name", "parent", "customer", "student", "email", "email_status", "email_error", "audience_source"],
		order_by="creation asc",
		limit=300,
	)
	return payload


def save_school_admin_announcement_data(announcement=None, payload=None):
	_require_school_admin()
	payload = _parse_payload(payload)
	if announcement:
		doc = frappe.get_doc(ANNOUNCEMENT_DOCTYPE, announcement)
		if doc.status in {"Published", "Archived"}:
			frappe.throw(_("Published or archived announcements cannot be edited. Create a new draft instead."))
	else:
		doc = frappe.new_doc(ANNOUNCEMENT_DOCTYPE)
		doc.status = "Draft"

	_apply_announcement_payload(doc, payload)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return get_school_admin_announcement_data(doc.name)


def publish_school_admin_announcement_data(announcement=None):
	_require_school_admin()
	if not announcement:
		frappe.throw(_("Announcement is required."))
	doc = frappe.get_doc(ANNOUNCEMENT_DOCTYPE, announcement)
	if doc.status == "Published":
		return get_school_admin_announcement_data(doc.name)
	if doc.status == "Archived":
		frappe.throw(_("Archived announcements cannot be published."))

	_validate_announcement(doc)
	recipients = _resolve_announcement_recipients(doc)
	if not recipients:
		frappe.throw(_("No parent recipients matched this announcement audience."))

	_delete_existing_recipients(doc.name)
	email_requested = bool(cint(doc.send_email_on_publish))
	email_enabled = outbound_email_enabled()
	created = []
	for recipient in recipients:
		row = frappe.new_doc(RECIPIENT_DOCTYPE)
		row.announcement = doc.name
		row.parent = recipient.get("parent")
		row.customer = recipient.get("customer")
		row.student = recipient.get("student")
		row.linked_user = recipient.get("linked_user")
		row.email = recipient.get("email")
		row.audience_source = recipient.get("audience_source")
		row.source_document = recipient.get("source_document")
		row.email_status = "Queued" if email_requested and email_enabled and recipient.get("email") else "Not Requested"
		if email_requested and not email_enabled and recipient.get("email"):
			row.email_status = "Skipped"
			row.email_error = email_block_reason()
		if email_requested and not recipient.get("email"):
			row.email_status = "Failed"
			row.email_error = "No parent email found."
		row.insert(ignore_permissions=True)
		created.append(row.name)

	doc.status = "Published"
	doc.published_by = frappe.session.user
	doc.published_at = now_datetime()
	doc.publish_at = doc.publish_at or doc.published_at
	doc.recipient_count = len(created)
	doc.email_queued_count = len([name for name in created if frappe.db.get_value(RECIPIENT_DOCTYPE, name, "email_status") == "Queued"])
	doc.save(ignore_permissions=True)

	if email_requested and email_enabled and doc.email_queued_count:
		frappe.enqueue(
			"qas_custom.services.announcements.send_school_announcement_email_job",
			queue="short",
			timeout=600,
			enqueue_after_commit=True,
			announcement=doc.name,
		)

	frappe.db.commit()
	return get_school_admin_announcement_data(doc.name)


def archive_school_admin_announcement_data(announcement=None):
	_require_school_admin()
	if not announcement:
		frappe.throw(_("Announcement is required."))
	frappe.db.set_value(ANNOUNCEMENT_DOCTYPE, announcement, "status", "Archived", update_modified=True)
	frappe.db.commit()
	return get_school_admin_announcement_data(announcement)


def get_parent_announcements_data(limit=30):
	parent = _require_parent()
	if not _announcement_available():
		return {"items": []}

	recipient_rows = frappe.get_all(
		RECIPIENT_DOCTYPE,
		filters={"parent": parent.name},
		fields=["name", "announcement", "student", "email_status"],
		order_by="creation desc",
		limit=_limit(limit, default=30, max_value=100),
	)
	announcement_ids = [row.announcement for row in recipient_rows if row.get("announcement")]
	if not announcement_ids:
		return {"items": []}

	announcement_rows = frappe.get_all(
		ANNOUNCEMENT_DOCTYPE,
		filters={"name": ["in", announcement_ids], "status": "Published"},
		fields=["name", "title", "body", "audience_type", "publish_at", "expires_at", "published_at"],
		order_by="publish_at desc, published_at desc",
	)
	by_name = {row.name: row for row in announcement_rows if _is_parent_visible_announcement(row)}
	items = []
	seen = set()
	for recipient in recipient_rows:
		announcement = by_name.get(recipient.announcement)
		if not announcement or announcement.name in seen:
			continue
		seen.add(announcement.name)
		items.append(
			{
				"name": announcement.name,
				"title": announcement.title,
				"body": announcement.body,
				"audience_type": announcement.audience_type,
				"publish_at": announcement.publish_at,
				"published_at": announcement.published_at,
			}
		)
	return {"items": items}


def send_school_announcement_email_job(announcement: str):
	if not announcement or not frappe.db.exists(ANNOUNCEMENT_DOCTYPE, announcement):
		return {"sent": 0, "failed": 0}
	doc = frappe.get_doc(ANNOUNCEMENT_DOCTYPE, announcement)
	rows = frappe.get_all(
		RECIPIENT_DOCTYPE,
		filters={"announcement": announcement, "email_status": "Queued"},
		fields=["name", "email"],
		limit=0,
	)
	sent = 0
	failed = 0
	grouped_rows = {}
	missing_email_rows = []
	for row in rows:
		email = str(row.get("email") or "").strip()
		if not email:
			missing_email_rows.append(row.get("name"))
			continue
		group = grouped_rows.setdefault(email.lower(), {"email": email, "rows": []})
		group["rows"].append(row.get("name"))

	if missing_email_rows:
		_set_announcement_recipient_email_status(
			missing_email_rows,
			"Failed",
			error="No parent email found.",
		)
		failed += len(missing_email_rows)
		frappe.db.commit()

	groups = list(grouped_rows.values())
	for start in range(0, len(groups), ANNOUNCEMENT_BCC_BATCH_SIZE):
		batch = groups[start : start + ANNOUNCEMENT_BCC_BATCH_SIZE]
		batch_emails = [group["email"] for group in batch]
		batch_rows = [row_name for group in batch for row_name in group["rows"]]
		try:
			result = sendmail_or_skip(
				action="school_announcement_email",
				recipients=[],
				bcc=batch_emails,
				subject=doc.email_subject or doc.title,
				message=_announcement_email_message(doc),
				reference_doctype=ANNOUNCEMENT_DOCTYPE,
				reference_name=doc.name,
			)
			if result and result.get("skipped"):
				_set_announcement_recipient_email_status(
					batch_rows,
					"Skipped",
					error=result.get("reason") or email_block_reason(),
				)
			else:
				_set_announcement_recipient_email_status(
					batch_rows,
					"Sent",
					sent_at=now_datetime(),
				)
				sent += len(batch_rows)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"QAS announcement email failed: {announcement}")
			_set_announcement_recipient_email_status(
				batch_rows,
				"Failed",
				error="Email send failed.",
			)
			failed += len(batch_rows)
		frappe.db.commit()
	return {"sent": sent, "failed": failed}


def _set_announcement_recipient_email_status(row_names, status, error="", sent_at=None):
	if not row_names:
		return
	values = {"email_status": status, "email_error": error}
	if sent_at:
		values["email_sent_at"] = sent_at
	frappe.db.set_value(
		RECIPIENT_DOCTYPE,
		{"name": ["in", row_names]},
		values,
		update_modified=True,
	)


def _announcement_email_message(doc):
	body = _message_html(doc.email_body or doc.body or "")
	link = f"{_parent_portal_base_url()}/announcements"
	return f"""
		<div style="font-family:Arial,sans-serif;color:#1a2b4a;line-height:1.55;">
			<h2>{escape_html(doc.title)}</h2>
			<div>{body}</div>
			<p style="margin-top:20px;">
				<a href="{link}" style="display:inline-block;background:#1a2b4a;color:white;padding:10px 14px;border-radius:8px;text-decoration:none;">View in Parent Portal</a>
			</p>
		</div>
	"""


def _message_html(value):
	text = str(value or "")
	if "<" in text and ">" in text:
		return text
	return escape_html(text).replace("\n", "<br>")


def _parent_portal_base_url():
	base_url = (
		frappe.conf.get("qas_parent_portal_url")
		or frappe.conf.get("parent_portal_url")
		or DEFAULT_PARENT_PORTAL_URL
	)
	return str(base_url).rstrip("/")


def _apply_announcement_payload(doc, payload):
	for fieldname in [
		"title",
		"audience_type",
		"term",
		"course",
		"course_session",
		"student",
		"body",
		"publish_at",
		"expires_at",
		"send_email_on_publish",
		"email_subject",
		"email_body",
	]:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname))
	_validate_announcement(doc)


def _validate_announcement(doc):
	if not doc.title:
		frappe.throw(_("Announcement title is required."))
	if not doc.body:
		frappe.throw(_("Announcement body is required."))
	if doc.audience_type == "Term" and not doc.term:
		frappe.throw(_("Term is required for a term announcement."))
	if doc.audience_type == "Term + Course" and (not doc.term or not doc.course):
		frappe.throw(_("Term and course are required for a term course announcement."))
	if doc.audience_type == "Course Session" and not doc.course_session:
		frappe.throw(_("Course session is required for a course session announcement."))
	if doc.audience_type == "Single Student" and not doc.student:
		frappe.throw(_("Student is required for a single student announcement."))


def _resolve_announcement_recipients(doc):
	if doc.audience_type == "All Parents":
		return _all_parent_recipients()
	if doc.audience_type == "Term":
		return _enrollment_parent_recipients({"term": doc.term})
	if doc.audience_type == "Term + Course":
		return _enrollment_parent_recipients({"term": doc.term, "course": doc.course})
	if doc.audience_type == "Course Session":
		return _session_parent_recipients(doc.course_session)
	if doc.audience_type == "Single Student":
		return _dedupe_recipients([_single_student_recipient(doc.student)])
	return []


def _single_student_recipient(student):
	if not student or not frappe.db.exists("Student", student):
		frappe.throw(_("The selected Student was not found."))
	parent = _student_parent(student)
	if not parent:
		frappe.throw(_("The selected Student is not linked to a Parent/Family."))
	if not frappe.db.exists("Parent", parent):
		frappe.throw(_("The Parent/Family linked to the selected Student was not found."))
	return _recipient_from_parent_name(
		parent,
		student=student,
		audience_source="Single Student",
		source_document=student,
	)


def _all_parent_recipients():
	fields = _parent_fields()
	filters = {}
	if frappe.db.has_column("Parent", "status"):
		filters["status"] = ["!=", "Inactive"]
	rows = frappe.get_all("Parent", filters=filters, fields=fields, limit=0)
	return [_recipient_from_parent(row, audience_source="All Parents", source_document="") for row in rows]


def _enrollment_parent_recipients(filters):
	query_filters = dict(filters)
	query_filters["status"] = ["in", ["Planned", "Active"]]
	rows = frappe.get_all(
		"Enrollment",
		filters=query_filters,
		fields=["name", "student", "parent", "term", "course", "status"],
		limit=0,
	)
	recipients = []
	for row in rows:
		parent = row.parent or _student_parent(row.student)
		recipients.append(_recipient_from_parent_name(parent, student=row.student, audience_source="Enrollment", source_document=row.name))
	return _dedupe_recipients(recipients)


def _session_parent_recipients(course_session):
	filters = {"course_session": course_session}
	if frappe.db.has_column("Class Attendance Entry", "status"):
		filters["status"] = ["!=", "Cancelled"]
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters=filters,
		fields=["name", "student", "enrollment_type", "source_doctype", "source_document"],
		limit=0,
	)
	recipients = []
	for row in rows:
		parent = _student_parent(row.student)
		recipients.append(_recipient_from_parent_name(parent, student=row.student, audience_source=row.enrollment_type or "Course Session", source_document=row.name))
	return _dedupe_recipients(recipients)


def _recipient_from_parent_name(parent, **extra):
	if not parent:
		return None
	row = frappe.db.get_value("Parent", parent, _parent_fields(), as_dict=True)
	if not row:
		return None
	return _recipient_from_parent(row, **extra)


def _recipient_from_parent(row, student=None, audience_source="", source_document=""):
	if not row:
		return None
	linked_user = row.get("linked_user")
	email = _first_value(row, ["email", "email_id", "contact_email"])
	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user
	return {
		"parent": row.get("name"),
		"customer": row.get("customer"),
		"student": student,
		"linked_user": linked_user,
		"email": email,
		"audience_source": audience_source,
		"source_document": source_document,
	}


def _dedupe_recipients(rows):
	seen = set()
	items = []
	for row in rows:
		if not row:
			continue
		key = row.get("parent") or row.get("email")
		if not key or key in seen:
			continue
		seen.add(key)
		items.append(row)
	return items


def _student_parent(student):
	if not student:
		return None
	for fieldname in ("guardian", "parent"):
		if frappe.db.has_column("Student", fieldname):
			value = frappe.db.get_value("Student", student, fieldname)
			if value:
				return value
	return None


def _student_search_fields():
	fields = ["name"]
	for fieldname in [
		"student_name",
		"first_name",
		"last_name",
		"student_code",
		"date_of_birth",
		"status",
		"guardian",
		"parent",
	]:
		if frappe.db.has_column("Student", fieldname):
			fields.append(fieldname)
	return fields


def _student_search_rank(row, query):
	needle = str(query or "").strip().lower()
	values = [str(row.get(field) or "").strip().lower() for field in ["name", "student_name", "student_code", "first_name", "last_name"]]
	if needle in values:
		match_rank = 0
	elif any(value.startswith(needle) for value in values if value):
		match_rank = 1
	else:
		match_rank = 2
	display = str(row.get("student_name") or row.get("name") or "").lower()
	return match_rank, display, str(row.get("name") or "").lower()


def _announcement_student_preview(row):
	student = row.get("name")
	parent = row.get("guardian") or row.get("parent") or _student_parent(student)
	parent_row = frappe.db.get_value("Parent", parent, _parent_fields(), as_dict=True) if parent else None
	email = ""
	if parent_row:
		email = _first_value(parent_row, ["email", "email_id", "contact_email"])
		if not email and parent_row.get("linked_user"):
			email = frappe.db.get_value("User", parent_row.get("linked_user"), "email") or parent_row.get("linked_user")
	return {
		"student": student,
		"student_name": row.get("student_name") or student,
		"student_code": row.get("student_code"),
		"date_of_birth": row.get("date_of_birth"),
		"status": row.get("status"),
		"parent": parent,
		"parent_name": parent_row.get("parent_name") if parent_row else "",
		"parent_email": email or "",
		"customer": parent_row.get("customer") if parent_row else "",
		"eligible": bool(parent_row),
		"error": "" if parent_row else "No linked Parent/Family found.",
	}


def _parent_fields():
	fields = ["name"]
	for fieldname in ["parent_name", "customer", "linked_user", "email", "email_id", "contact_email", "status"]:
		if frappe.db.has_column("Parent", fieldname):
			fields.append(fieldname)
	return fields


def _require_school_admin():
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)


def _require_parent():
	support_parent = get_support_view_parent()
	if support_parent:
		return support_parent
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
	if not parent_name:
		frappe.throw(_("No parent record is linked to this account."), frappe.PermissionError)
	return frappe.get_cached_doc("Parent", parent_name)


def _is_parent_visible_announcement(row):
	now = now_datetime()
	publish_at = _as_datetime(row.get("publish_at"))
	expires_at = _as_datetime(row.get("expires_at"))
	if publish_at and publish_at > now:
		return False
	if expires_at and expires_at < now:
		return False
	return True


def _as_datetime(value):
	if not value:
		return None
	try:
		return get_datetime(value)
	except Exception:
		return None


def _delete_existing_recipients(announcement):
	for row in frappe.get_all(RECIPIENT_DOCTYPE, filters={"announcement": announcement}, pluck="name", limit=0):
		frappe.delete_doc(RECIPIENT_DOCTYPE, row, ignore_permissions=True)


def _doc_payload(doc):
	data = {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields if field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "Button", "HTML"}}
	data["name"] = doc.name
	return data


def _normalise_row(row):
	return dict(row)


def _parse_payload(payload):
	if isinstance(payload, str):
		return json.loads(payload or "{}")
	return payload or {}


def _announcement_available():
	return frappe.db.table_exists(ANNOUNCEMENT_DOCTYPE) and frappe.db.table_exists(RECIPIENT_DOCTYPE)


def _limit(value, default=50, max_value=200):
	try:
		parsed = int(value or default)
	except (TypeError, ValueError):
		parsed = default
	return max(1, min(parsed, max_value))


def _first_value(row, fields):
	for field in fields:
		if row.get(field):
			return row.get(field)
	return None
