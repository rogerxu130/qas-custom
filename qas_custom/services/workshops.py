from __future__ import annotations

import json
import mimetypes
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, today
from frappe.utils.file_manager import save_file

from qas_custom.modules.billing.commands import (
	get_invoice_customer,
	run_invoice_mutation_as_administrator,
	sync_invoice_student_summary,
)
from qas_custom.modules.billing.invoice_settings import apply_default_invoice_dates, apply_invoice_payment_snapshot
from qas_custom.modules.common import has_field, set_if_field
from qas_custom.modules.notifications.guard import disable_sales_invoice_auto_notifications
from qas_custom.services.display_labels import get_student_display_code, get_student_parent_name
from qas_custom.services.support_view import get_support_view_parent, get_support_view_teacher, reject_support_view_write


ADMIN_ROLES = {"School Admin", "System Manager"}
ATTENDANCE_STATUSES = ("Not Marked", "Present", "Absent", "Late", "Cancelled")
MAX_PHOTO_UPLOADS = 12
MAX_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024


# School Admin

def get_school_admin_workshop_offerings_data(query=None, category=None, campus=None, status=None, limit=120):
	_require_school_admin()
	filters = {}
	if category:
		filters["workshop_category"] = category
	if campus:
		filters["campus"] = campus
	if status:
		filters["status"] = status
	or_filters = None
	if (query or "").strip():
		text = f"%{query.strip()}%"
		or_filters = {"name": ["like", text], "title": ["like", text]}
	rows = frappe.get_all(
		"Workshop Offering",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "title", "workshop_category", "participation_mode", "class_language", "campus", "standard_price", "capacity", "status", "modified"],
		order_by="modified desc",
		limit=min(max(cint(limit or 120), 1), 500),
	)
	counts = _offering_counts([row.name for row in rows])
	return {"items": [{**dict(row), **counts.get(row.name, {})} for row in rows]}


def get_school_admin_workshop_offering_data(workshop_offering=None):
	_require_school_admin()
	return _build_offering_detail(_required_doc("Workshop Offering", workshop_offering))


def save_school_admin_workshop_offering_data(workshop_offering=None, payload=None):
	_require_school_admin()
	payload = _payload(payload)
	sessions = payload.pop("sessions", None)
	requested_status = payload.pop("status", None)
	doc = frappe.get_doc("Workshop Offering", workshop_offering) if workshop_offering else frappe.new_doc("Workshop Offering")
	for field in (
		"title", "workshop_category", "participation_mode", "class_language", "campus", "description",
		"minimum_age", "maximum_age", "standard_price", "capacity", "materials_or_preparation", "inclusions",
	):
		if field in payload:
			doc.set(field, payload.get(field))
	if doc.is_new():
		doc.status = "Draft"
	doc.save(ignore_permissions=True)
	if sessions is not None:
		_replace_offering_sessions(doc, sessions)
	if requested_status is not None:
		doc.status = requested_status
		doc.save(ignore_permissions=True)
	_sync_session_positions(doc.name)
	frappe.db.commit()
	return {"offering": _build_offering_detail(doc)}


def duplicate_school_admin_workshop_offering_data(workshop_offering=None):
	_require_school_admin()
	source = _required_doc("Workshop Offering", workshop_offering)
	savepoint = "duplicate_workshop_offering"
	frappe.db.savepoint(savepoint)
	try:
		copy = frappe.new_doc("Workshop Offering")
		for field in (
			"workshop_category", "participation_mode", "class_language", "campus", "description",
			"minimum_age", "maximum_age", "standard_price", "capacity", "materials_or_preparation", "inclusions",
		):
			copy.set(field, source.get(field))
		copy.title = _("Copy of {0}").format(source.title)
		copy.status = "Draft"
		copy.insert(ignore_permissions=True)

		sessions = frappe.get_all(
			"Workshop Session",
			filters={"workshop_offering": source.name},
			fields=["session_date", "start_time", "end_time", "teacher", "classroom", "status"],
			order_by="session_date asc, start_time asc, name asc",
		)
		for values in sessions:
			session = frappe.new_doc("Workshop Session")
			session.workshop_offering = copy.name
			for field in ("session_date", "start_time", "end_time", "teacher", "classroom", "status"):
				session.set(field, values.get(field))
			session.insert(ignore_permissions=True)

		_sync_session_positions(copy.name)
		frappe.db.commit()
		return {"source": source.name, "offering": _build_offering_detail(copy)}
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


def create_school_admin_workshop_enrollment_data(payload=None):
	_require_school_admin()
	payload = _payload(payload)
	doc = frappe.get_doc({
		"doctype": "Workshop Enrollment",
		"workshop_offering": payload.get("workshop_offering"),
		"student": payload.get("student"),
		"parent": payload.get("parent"),
		"status": "Planned",
		"enrollment_date": payload.get("enrollment_date") or today(),
		"adult_participant_name": payload.get("adult_participant_name"),
		"adult_participant_parent": payload.get("adult_participant_parent"),
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"enrollment": _enrollment_payload(doc)}


def activate_school_admin_workshop_enrollment_data(workshop_enrollment=None):
	_require_school_admin()
	doc = _required_doc("Workshop Enrollment", workshop_enrollment)
	if doc.status not in {"Planned", "Active"}:
		frappe.throw(_("Only Planned or Active Workshop Enrollments can prepare Attendance."))
	sessions = frappe.get_all("Workshop Session", filters={"workshop_offering": doc.workshop_offering, "status": ["!=", "Cancelled"]}, fields=["name"], order_by="session_date asc, start_time asc")
	if not sessions:
		frappe.throw(_("The Workshop Offering has no active Sessions."))
	doc.status = "Active"
	doc.save(ignore_permissions=True)
	created = 0
	for session in sessions:
		if frappe.db.exists("Workshop Attendance", {"workshop_session": session.name, "workshop_enrollment": doc.name}):
			continue
		frappe.get_doc({
			"doctype": "Workshop Attendance", "workshop_session": session.name,
			"workshop_enrollment": doc.name, "student": doc.student, "status": "Not Marked",
		}).insert(ignore_permissions=True)
		created += 1
	frappe.db.commit()
	return {"enrollment": _enrollment_payload(doc), "attendance_entries": created}


def cancel_school_admin_workshop_enrollment_data(workshop_enrollment=None):
	_require_school_admin()
	doc = _required_doc("Workshop Enrollment", workshop_enrollment)
	if doc.status not in {"Planned", "Active"}:
		frappe.throw(_("Only Planned or Active Workshop Enrollments can be cancelled."))
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	rows = frappe.get_all("Workshop Attendance", filters={"workshop_enrollment":doc.name}, pluck="name")
	for name in rows:
		attendance = frappe.get_doc("Workshop Attendance", name)
		attendance.previous_status = attendance.status
		attendance.status = "Cancelled"
		attendance.marked_by = frappe.session.user
		attendance.marked_at = now_datetime()
		attendance.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"enrollment": _enrollment_payload(doc),
		"attendance_entries": len(rows),
		"billing_warning": _("Review and cancel or credit the linked Invoice separately.") if doc.invoice else "",
	}


def create_school_admin_workshop_invoice_data(workshop_enrollment=None):
	_require_school_admin()
	enrollment = _required_doc("Workshop Enrollment", workshop_enrollment)
	if enrollment.status != "Active":
		frappe.throw(_("Activate the Workshop Enrollment before creating an invoice."))
	existing = enrollment.invoice or frappe.db.get_value("Sales Invoice", {"source_doctype": "Workshop Enrollment", "source_document": enrollment.name, "docstatus": ["<", 2]}, "name")
	if existing:
		frappe.throw(_("This Workshop Enrollment already has an invoice: {0}.").format(existing))
	offering = _required_doc("Workshop Offering", enrollment.workshop_offering)
	customer = get_invoice_customer(enrollment.parent)
	item_code = _workshop_invoice_item()
	dates = frappe.get_all("Workshop Session", filters={"workshop_offering": offering.name, "status": ["!=", "Cancelled"]}, pluck="session_date", order_by="session_date asc")
	date_label = ", ".join(str(value) for value in dates)
	student_name = get_student_parent_name(enrollment.student) or enrollment.student
	description = f"{offering.title}\n{student_name}\n{offering.campus}\n{date_label}"
	disable_sales_invoice_auto_notifications()
	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	apply_default_invoice_dates(invoice)
	set_if_field(invoice, "parent", enrollment.parent)
	set_if_field(invoice, "qas_invoice_type", "Workshop")
	set_if_field(invoice, "source_doctype", "Workshop Enrollment")
	set_if_field(invoice, "source_document", enrollment.name)
	set_if_field(invoice, "primary_student", enrollment.student)
	set_if_field(invoice, "billing_note", _("Draft Workshop invoice. Review and apply any manual discount before submitting."))
	item = invoice.append("items", {"item_code": item_code, "item_name": offering.title, "description": description, "qty": 1, "rate": flt(enrollment.standard_price_snapshot)})
	set_if_field(item, "qas_line_type", "Workshop")
	set_if_field(item, "student", enrollment.student)
	set_if_field(item, "student_display_name", student_name)
	set_if_field(item, "student_code", get_student_display_code(enrollment.student) or enrollment.student)
	set_if_field(item, "enrollment", enrollment.name)
	sync_invoice_student_summary(invoice)
	apply_invoice_payment_snapshot(invoice)
	run_invoice_mutation_as_administrator(lambda: invoice.insert(ignore_permissions=True))
	enrollment.invoice = invoice.name
	enrollment.invoice_status = "Draft"
	enrollment.invoice_amount = invoice.grand_total
	enrollment.save(ignore_permissions=True)
	frappe.db.commit()
	return {"enrollment": _enrollment_payload(enrollment), "invoice": invoice.name}


def update_school_admin_workshop_attendance_data(workshop_session=None, updates=None):
	_require_school_admin()
	_update_attendance(workshop_session, updates, teacher=None)
	frappe.db.commit()
	return get_school_admin_workshop_session_data(workshop_session)


def get_school_admin_workshop_session_data(workshop_session=None):
	_require_school_admin()
	return _build_session_detail(_required_doc("Workshop Session", workshop_session), include_drafts=True)


# Teacher

def get_teacher_workshop_sessions_data(from_date=None, to_date=None):
	teacher = _require_teacher()
	filters = {"teacher": teacher.name, "status": ["!=", "Cancelled"]}
	if from_date and to_date:
		filters["session_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["session_date"] = [">=", getdate(from_date)]
	else:
		filters["session_date"] = ["between", [getdate(today()), getdate(add_days(today(), 14))]]
	rows = frappe.get_all("Workshop Session", filters=filters, fields=["name", "workshop_offering", "session_date", "start_time", "end_time", "campus", "classroom", "status", "workshop_session_index", "workshop_session_count"], order_by="session_date asc, start_time asc")
	offerings = _offering_map([row.workshop_offering for row in rows])
	counts = _attendance_counts([row.name for row in rows])
	items = []
	for row in rows:
		offering = offerings.get(row.workshop_offering, {})
		items.append({
			"id": row.name, "session_id": row.name, "source_type": "workshop", "session_date": str(row.session_date),
			"start_time": str(row.start_time), "end_time": str(row.end_time), "course": offering.get("title"),
			"workshop_category": offering.get("workshop_category"), "class_language": offering.get("class_language"),
			"campus": row.campus, "classroom": row.classroom, "status": row.status,
			"student_count": counts.get(row.name, 0), "leave_count": 0,
			"workshop_session_index": row.workshop_session_index, "workshop_session_count": row.workshop_session_count,
		})
	return {"items": items}


def get_teacher_workshop_session_detail_data(workshop_session=None):
	teacher = _require_teacher()
	session = _required_doc("Workshop Session", workshop_session)
	_assert_teacher_session(session, teacher.name)
	return _build_session_detail(session, include_drafts=True)


def update_teacher_workshop_attendance_data(workshop_session=None, updates=None):
	reject_support_view_write()
	teacher = _require_teacher()
	session = _required_doc("Workshop Session", workshop_session)
	_assert_teacher_session(session, teacher.name)
	_update_attendance(session.name, updates, teacher=teacher.name)
	frappe.db.commit()
	return _build_session_detail(session, include_drafts=True)


def publish_teacher_workshop_homework_data(workshop_session=None, title=None, description=None):
	reject_support_view_write()
	teacher = _require_teacher()
	payload = _request_json()
	workshop_session = workshop_session or payload.get("workshop_session")
	title = (title or payload.get("title") or "").strip()
	if not title:
		frappe.throw(_("Homework title is required."))
	session = _required_doc("Workshop Session", workshop_session)
	_assert_teacher_session(session, teacher.name)
	doc = frappe.get_doc({"doctype":"Workshop Homework", "workshop_session":session.name, "title":title, "description":description if description is not None else payload.get("description"), "teacher":teacher.name, "status":"Published", "published_at":now_datetime()})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"homework": _homework_payload(doc.as_dict())}


def publish_teacher_workshop_photo_post_data(workshop_session=None, title=None, caption=None):
	reject_support_view_write()
	teacher = _require_teacher()
	form = _request_form()
	workshop_session = workshop_session or form.get("workshop_session")
	session = _required_doc("Workshop Session", workshop_session)
	_assert_teacher_session(session, teacher.name)
	uploads = _uploaded_files("photos", "photo")
	if not uploads or len(uploads) > MAX_PHOTO_UPLOADS:
		frappe.throw(_("Upload between 1 and {0} photos.").format(MAX_PHOTO_UPLOADS))
	doc = frappe.get_doc({"doctype":"Workshop Photo Post", "workshop_session":session.name, "title":(title or form.get("title") or "Workshop Photos").strip(), "caption":caption if caption is not None else form.get("caption"), "teacher":teacher.name, "status":"Draft"})
	doc.insert(ignore_permissions=True)
	for upload in uploads:
		filename = _upload_filename(upload, "workshop-photo.jpg")
		if not ((getattr(upload, "mimetype", "") or "").startswith("image/") or filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"))):
			frappe.throw(_("Only image files can be uploaded."))
		content = _read_upload(upload)
		file_doc = save_file(filename, content, "Workshop Photo Post", doc.name, is_private=1)
		doc.append("photos", {"photo":file_doc.file_url, "preview_url":file_doc.file_url, "file_name":file_doc.file_name, "file_size":len(content), "mime_type":getattr(upload, "mimetype", "")})
	doc.status = "Published"
	doc.posted_at = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"photo_post": _photo_post_payload(doc, "teacher")}


def publish_teacher_workshop_video_post_data(workshop_session=None, title=None, caption=None):
	reject_support_view_write()
	teacher = _require_teacher()
	form = _request_form()
	workshop_session = workshop_session or form.get("workshop_session")
	session = _required_doc("Workshop Session", workshop_session)
	_assert_teacher_session(session, teacher.name)
	uploads = _uploaded_files("video", "videos")
	if not uploads:
		frappe.throw(_("A video file is required."))
	upload = uploads[0]
	filename = _upload_filename(upload, "workshop-video.mp4")
	if not ((getattr(upload, "mimetype", "") or "").startswith("video/") or filename.lower().endswith((".mp4", ".mov", ".webm"))):
		frappe.throw(_("Only MP4, MOV, or WebM video files can be uploaded."))
	content = _read_upload(upload)
	if len(content) > MAX_VIDEO_UPLOAD_BYTES:
		frappe.throw(_("Please upload a video smaller than 100 MB."))
	doc = frappe.get_doc({"doctype":"Workshop Video Post", "workshop_session":session.name, "title":(title or form.get("title") or "Workshop Video").strip(), "caption":caption if caption is not None else form.get("caption"), "teacher":teacher.name, "status":"Draft"})
	doc.insert(ignore_permissions=True)
	file_doc = save_file(filename, content, "Workshop Video Post", doc.name, is_private=1)
	doc.video, doc.file_name, doc.file_size, doc.mime_type = file_doc.file_url, file_doc.file_name, len(content), getattr(upload, "mimetype", "")
	doc.status, doc.posted_at = "Published", now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"video_post": _video_post_payload(doc, "teacher")}


# Parent

def get_parent_workshops_data(student=None):
	parent = _require_parent()
	students = frappe.get_all("Student", filters={"guardian": parent.name}, fields=["name", "student_name"])
	student_ids = {row.name for row in students}
	if student:
		if student not in student_ids:
			frappe.throw(_("This Student is not linked to the current Parent."), frappe.PermissionError)
		student_ids = {student}
	if not student_ids:
		return {"items": []}
	enrollments = frappe.get_all("Workshop Enrollment", filters={"parent":parent.name, "student":["in", list(student_ids)], "status":["in", ["Active", "Completed"]]}, fields=["name", "workshop_offering", "student", "status", "invoice", "invoice_status", "invoice_amount", "adult_participant_name"], order_by="modified desc")
	items = []
	for enrollment in enrollments:
		offering = _build_offering_detail(_required_doc("Workshop Offering", enrollment.workshop_offering), include_drafts=False, audience="parent")
		offering["enrollment"] = _enrollment_payload(enrollment)
		items.append(offering)
	return {"items": items}


def get_workshop_photo_content_data(photo_post=None, photo_idx=None, audience="teacher"):
	post = _required_doc("Workshop Photo Post", photo_post)
	_authorize_content(post.workshop_session, post.status, audience)
	row = next((item for item in post.photos or [] if cint(item.idx) == cint(photo_idx)), None)
	return _file_response(row.photo if row else None)


def get_workshop_video_content_data(video_post=None, audience="teacher", download=False):
	post = _required_doc("Workshop Video Post", video_post)
	_authorize_content(post.workshop_session, post.status, audience)
	return _file_response(post.video, download=download, fallback=post.file_name, content_type=post.mime_type)


# Shared builders

def _build_offering_detail(doc, include_drafts=True, audience="admin"):
	sessions = frappe.get_all("Workshop Session", filters={"workshop_offering":doc.name}, fields=["name", "session_date", "start_time", "end_time", "teacher", "classroom", "campus", "workshop_session_index", "workshop_session_count", "status"], order_by="session_date asc, start_time asc")
	enrollments = frappe.get_all("Workshop Enrollment", filters={"workshop_offering":doc.name}, fields=["name", "student", "parent", "status", "enrollment_date", "standard_price_snapshot", "adult_participant_name", "invoice", "invoice_status", "invoice_amount"], order_by="creation asc") if audience == "admin" else []
	result = {field: doc.get(field) for field in ("name", "title", "workshop_category", "participation_mode", "class_language", "campus", "description", "minimum_age", "maximum_age", "standard_price", "capacity", "materials_or_preparation", "inclusions", "status")}
	result["sessions"] = [_session_payload(row) for row in sessions]
	result["enrollments"] = [_enrollment_payload(row) for row in enrollments]
	result["content"] = _content_for_sessions([row.name for row in sessions], include_drafts=include_drafts, audience=audience)
	return result


def _build_session_detail(session, include_drafts=True):
	offering = _required_doc("Workshop Offering", session.workshop_offering)
	attendance = frappe.get_all("Workshop Attendance", filters={"workshop_session":session.name}, fields=["name", "workshop_enrollment", "student", "status", "comments", "marked_by", "marked_at"], order_by="creation asc")
	student_map = {row.name: row for row in frappe.get_all("Student", filters={"name":["in", [item.student for item in attendance] or [""]]}, fields=["name", "student_name", "guardian", "teaching_notes"])}
	parent_ids = {row.guardian for row in student_map.values() if row.guardian}
	parent_map = {row.name: row for row in frappe.get_all("Parent", filters={"name":["in", list(parent_ids) or [""]]}, fields=["name", "parent_name", "mobile_number", "linked_user"])}
	students = []
	for row in attendance:
		student = student_map.get(row.student, {})
		parent = parent_map.get(student.get("guardian"), {})
		students.append({"row_id":row.name, "student":row.student, "student_name":student.get("student_name") or row.student, "teaching_notes":student.get("teaching_notes") or "", "parent_name":parent.get("parent_name") or "", "parent_phone":parent.get("mobile_number") or "", "parent_email":parent.get("linked_user") or "", "status":row.status, "comments":row.comments or "", "enrollment_type":"Workshop"})
	content = _content_for_sessions([session.name], include_drafts=include_drafts, audience="teacher")
	return {"session": {**_session_payload(session), "id":session.name, "session_id":session.name, "source_type":"workshop", "course":offering.title, "workshop_category":offering.workshop_category, "class_language":offering.class_language, "student_count":len(students), "leave_count":0}, "students":students, "homeworks":content["homeworks"], "photo_posts":content["photo_posts"], "video_posts":content["video_posts"], "status_options":list(ATTENDANCE_STATUSES)}


def _content_for_sessions(session_ids, include_drafts=False, audience="parent"):
	filters = {"workshop_session":["in", session_ids or [""]]}
	if not include_drafts:
		filters["status"] = "Published"
	homeworks = [_homework_payload(row) for row in frappe.get_all("Workshop Homework", filters=filters, fields=["name", "workshop_session", "title", "description", "attachment", "status", "published_at"], order_by="published_at desc")]
	photo_docs = [frappe.get_doc("Workshop Photo Post", name) for name in frappe.get_all("Workshop Photo Post", filters=filters, pluck="name", order_by="posted_at desc")]
	video_docs = [frappe.get_doc("Workshop Video Post", name) for name in frappe.get_all("Workshop Video Post", filters=filters, pluck="name", order_by="posted_at desc")]
	return {"homeworks":homeworks, "photo_posts":[_photo_post_payload(doc, audience) for doc in photo_docs], "video_posts":[_video_post_payload(doc, audience) for doc in video_docs]}


def _homework_payload(row):
	return {"id":row.get("name"), "workshop_session":row.get("workshop_session"), "title":row.get("title"), "description":row.get("description") or "", "attachment":row.get("attachment"), "status":row.get("status"), "published_at":str(row.get("published_at") or "")}


def _photo_post_payload(doc, audience):
	method = "qas_custom.api.teacher_portal.teacher_portal_get_workshop_photo_content" if audience == "teacher" else "qas_custom.api.parent_portal.parent_portal_get_workshop_photo_content"
	photos = [{"idx":row.idx, "preview_url":f"/api/method/{method}?{urlencode({'photo_post':doc.name, 'photo_idx':row.idx})}"} for row in doc.photos or []]
	return {"id":doc.name, "workshop_session":doc.workshop_session, "title":doc.title, "caption":doc.caption or "", "status":doc.status, "posted_at":str(doc.posted_at or ""), "photo_count":len(photos), "photos":photos, "remaining_photo_count":0}


def _video_post_payload(doc, audience):
	method = "qas_custom.api.teacher_portal.teacher_portal_get_workshop_video_content" if audience == "teacher" else "qas_custom.api.parent_portal.parent_portal_get_workshop_video_content"
	return {"id":doc.name, "workshop_session":doc.workshop_session, "title":doc.title, "caption":doc.caption or "", "status":doc.status, "posted_at":str(doc.posted_at or ""), "file_name":doc.file_name, "file_size":doc.file_size, "stream_url":f"/api/method/{method}?{urlencode({'video_post':doc.name})}"}


def _session_payload(row):
	return {"name":row.get("name"), "session_date":str(row.get("session_date") or ""), "start_time":_serialise_workshop_time(row.get("start_time")), "end_time":_serialise_workshop_time(row.get("end_time")), "teacher":row.get("teacher"), "classroom":row.get("classroom"), "campus":row.get("campus"), "workshop_session_index":row.get("workshop_session_index"), "workshop_session_count":row.get("workshop_session_count"), "status":row.get("status")}


def _serialise_workshop_time(value):
	if value in (None, ""):
		return ""
	if hasattr(value, "strftime"):
		return value.strftime("%H:%M:%S")
	text = str(value).strip()
	match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?", text)
	if not match:
		return text
	hour, minute, second = match.groups()
	return f"{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"


def _enrollment_payload(row):
	payload = {field: row.get(field) for field in ("name", "workshop_offering", "student", "parent", "status", "enrollment_date", "standard_price_snapshot", "adult_participant_name", "adult_participant_parent", "invoice", "invoice_status", "invoice_amount")}
	payload["student_name"] = get_student_parent_name(payload.get("student")) or payload.get("student")
	payload["parent_name"] = None
	if payload.get("parent"):
		payload["parent_name"] = frappe.db.get_value("Parent", payload.get("parent"), "parent_name") or payload.get("parent")
	if payload.get("invoice") and frappe.db.exists("Sales Invoice", payload["invoice"]):
		invoice = frappe.db.get_value("Sales Invoice", payload["invoice"], ["docstatus", "status", "grand_total", "outstanding_amount"], as_dict=True)
		if invoice:
			if cint(invoice.docstatus) == 2:
				payload["invoice_status"] = "Cancelled"
			elif cint(invoice.docstatus) == 0:
				payload["invoice_status"] = "Draft"
			elif flt(invoice.outstanding_amount) <= 0:
				payload["invoice_status"] = "Paid"
			else:
				payload["invoice_status"] = invoice.status or "Submitted"
			payload["invoice_amount"] = flt(invoice.grand_total)
	return payload


def _replace_offering_sessions(offering, sessions):
	if not isinstance(sessions, list):
		frappe.throw(_("Sessions must be a list."))
	existing = {row.name: row for row in frappe.get_all("Workshop Session", filters={"workshop_offering":offering.name}, fields=["name"])}
	kept = set()
	for values in sessions:
		if not isinstance(values, dict):
			frappe.throw(_("Each Workshop Session must be an object."))
		name = values.get("name")
		doc = frappe.get_doc("Workshop Session", name) if name and name in existing else frappe.new_doc("Workshop Session")
		doc.workshop_offering = offering.name
		for field in ("session_date", "start_time", "end_time", "teacher", "classroom", "status"):
			if field in values:
				doc.set(field, values.get(field))
		doc.save(ignore_permissions=True)
		kept.add(doc.name)
	for name in set(existing) - kept:
		frappe.delete_doc("Workshop Session", name, ignore_permissions=True)


def _sync_session_positions(offering):
	rows = frappe.get_all("Workshop Session", filters={"workshop_offering":offering, "status":["!=", "Cancelled"]}, fields=["name"], order_by="session_date asc, start_time asc")
	for index, row in enumerate(rows, 1):
		frappe.db.set_value("Workshop Session", row.name, {"workshop_session_index":index, "workshop_session_count":len(rows)}, update_modified=False)


def _update_attendance(workshop_session, updates, teacher=None):
	updates = _json_list(updates if updates is not None else _request_json().get("updates"))
	for update in updates:
		row = _required_doc("Workshop Attendance", update.get("row_id"))
		if row.workshop_session != workshop_session:
			frappe.throw(_("Attendance row does not belong to this Workshop Session."), frappe.PermissionError)
		status = update.get("status")
		if status not in ATTENDANCE_STATUSES:
			frappe.throw(_("Invalid Workshop Attendance status."))
		row.previous_status = row.status
		row.status = status
		row.comments = update.get("comments") or ""
		row.marked_by = frappe.session.user
		row.marked_at = now_datetime()
		row.save(ignore_permissions=True)


def _offering_counts(names):
	result = {name:{"session_count":0, "enrollment_count":0} for name in names}
	for row in frappe.get_all("Workshop Session", filters={"workshop_offering":["in", names or [""]], "status":["!=", "Cancelled"]}, fields=["workshop_offering"]):
		result[row.workshop_offering]["session_count"] += 1
	for row in frappe.get_all("Workshop Enrollment", filters={"workshop_offering":["in", names or [""]], "status":["in", ["Planned", "Active"]]}, fields=["workshop_offering"]):
		result[row.workshop_offering]["enrollment_count"] += 1
	return result


def _attendance_counts(session_ids):
	result = {name:0 for name in session_ids}
	for row in frappe.get_all("Workshop Attendance", filters={"workshop_session":["in", session_ids or [""]], "status":["!=", "Cancelled"]}, fields=["workshop_session"]):
		result[row.workshop_session] += 1
	return result


def _offering_map(names):
	return {row.name:dict(row) for row in frappe.get_all("Workshop Offering", filters={"name":["in", list(set(names)) or [""]]}, fields=["name", "title", "workshop_category", "class_language"])}


def _workshop_invoice_item():
	item = frappe.conf.get("qas_workshop_invoice_item") or frappe.conf.get("qas_default_invoice_item")
	if item and frappe.db.exists("Item", item):
		return item
	frappe.throw(_("Workshop invoice item is not configured. Set qas_workshop_invoice_item or qas_default_invoice_item."))


def _require_school_admin():
	if frappe.session.user == "Guest" or not set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can access Workshop administration."), frappe.PermissionError)


def _require_teacher():
	teacher = get_support_view_teacher()
	if teacher:
		return teacher
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	name = frappe.db.get_value("Teacher", {"user":frappe.session.user}, "name")
	if not name:
		frappe.throw(_("No Teacher record is linked to this account."), frappe.PermissionError)
	return frappe.get_cached_doc("Teacher", name)


def _require_parent():
	parent = get_support_view_parent()
	if parent:
		return parent
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	name = frappe.db.get_value("Parent", {"linked_user":frappe.session.user}, "name")
	if not name:
		frappe.throw(_("No Parent record is linked to this account."), frappe.PermissionError)
	return frappe.get_cached_doc("Parent", name)


def _assert_teacher_session(session, teacher):
	if session.teacher != teacher:
		frappe.throw(_("This Workshop Session is not assigned to you."), frappe.PermissionError)


def _authorize_content(workshop_session, status, audience):
	session = _required_doc("Workshop Session", workshop_session)
	if audience == "teacher":
		_assert_teacher_session(session, _require_teacher().name)
	else:
		if status != "Published":
			raise frappe.PermissionError
		parent = _require_parent()
		if not frappe.db.exists("Workshop Enrollment", {"workshop_offering":session.workshop_offering, "parent":parent.name, "status":["in", ["Active", "Completed"]]}):
			raise frappe.PermissionError


def _required_doc(doctype, name):
	if not name:
		frappe.throw(_("{0} is required.").format(doctype))
	return frappe.get_doc(doctype, name)


def _payload(value):
	if value is None:
		value = frappe.form_dict.get("payload")
	if isinstance(value, str):
		return json.loads(value) if value.strip() else {}
	return dict(value or {})


def _json_list(value):
	if isinstance(value, str):
		value = json.loads(value) if value.strip() else []
	if not isinstance(value, list):
		frappe.throw(_("Updates must be a list."))
	return [row for row in value if isinstance(row, dict)]


def _request_json():
	request = getattr(frappe.local, "request", None)
	if not request:
		return {}
	try:
		return request.get_json(silent=True) or {}
	except Exception:
		return {}


def _request_form():
	request = getattr(frappe.local, "request", None)
	return dict(getattr(request, "form", None) or {}) if request else {}


def _uploaded_files(*fieldnames):
	request = getattr(frappe.local, "request", None)
	files = getattr(request, "files", None) if request else None
	if not files:
		return []
	result = []
	if hasattr(files, "getlist"):
		for field in fieldnames:
			result.extend(files.getlist(field))
	return [item for item in (result or list(files.values())) if item]


def _upload_filename(upload, fallback):
	return (getattr(upload, "filename", None) or fallback).strip()


def _read_upload(upload):
	content = upload.stream.read() if getattr(upload, "stream", None) else upload.read()
	if not content:
		frappe.throw(_("Uploaded file is empty."))
	return content


def _file_response(file_url, download=False, fallback=None, content_type=None):
	if not file_url:
		raise frappe.DoesNotExistError
	name = frappe.db.get_value("File", {"file_url":file_url}, "name")
	if not name:
		raise frappe.DoesNotExistError
	doc = frappe.get_doc("File", name)
	filename = doc.file_name or fallback or file_url.rsplit("/", 1)[-1]
	return {"filename":filename, "content":doc.get_content(), "content_type":content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream", "display_content_as":"attachment" if cint(download) else "inline"}
