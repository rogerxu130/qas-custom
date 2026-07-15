from __future__ import annotations

import hashlib
import json
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, escape_html, now_datetime

from qas_custom.services.password_reset import (
	PASSWORD_RESET_TOKEN_DOCTYPE,
	PORTAL_PARENT,
	PORTAL_TEACHER,
	_build_password_reset_link,
)
from qas_custom.services.display_labels import get_student_display_name
from qas_custom.services.school_admin import _doctype_available, _safe_fields, _require_school_admin
from qas_custom.utils.environment import sendmail_or_skip

PORTAL_INVITE_EXPIRY_DAYS = 7
PARENT_PORTAL_ROLE = "Parent"
INVITE_LOG_DOCTYPE = "Parent Portal Invite Log"
PARENT_PORTAL_INVITE_LOG_SOURCES = {"Manual", "Bulk Never Invited"}
TEACHER_INVITE_COMMENT_MARKER = "Teacher Portal invite sent"
TERM_PARENT_INVITE_JOB_TTL_SECONDS = 86400
TERM_PARENT_INVITE_OPEN_ENROLLMENT_STATUSES = ["Planned", "Active"]
TERM_PARENT_INVITE_MAX_RECIPIENTS = 1000
PARENT_PORTAL_QUICK_GUIDE_URL = "https://drive.google.com/file/d/1Zcqctss-iaodYFSIUBrAsilbSoA8B4kA/view?usp=drive_link"
TERM_PARENT_INVITE_SCOPE_OPTIONS = {
	"term": {"label": _("Selected term parents")},
	"active_parents": {"label": _("All active/current parents")},
	"all_parents": {"label": _("All parents")},
}
TERM_PARENT_INVITE_STATUS_OPTIONS = {
	"never_invited": {"label": _("Never invited"), "statuses": {"never_invited"}},
	"invited_not_logged_in": {"label": _("Invited, not logged in"), "statuses": {"invited_not_logged_in"}},
	"never_logged_in": {"label": _("Never logged in"), "statuses": {"never_invited", "invited_not_logged_in"}},
	"no_email": {"label": _("No email"), "statuses": {"no_email"}},
	"logged_in": {"label": _("Logged in"), "statuses": {"logged_in"}},
}
TERM_PARENT_INVITE_SEND_MODES = {"never_invited", "invited_not_logged_in", "never_logged_in"}


def invite_parent_to_portal_data(parent=None):
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	result = _invite_parent_to_portal(parent, source="Manual")
	frappe.db.commit()
	return result


def bulk_invite_parents_to_portal_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	parents = payload.get("parents") or []
	parents = [str(parent).strip() for parent in parents if str(parent or "").strip()]
	if not parents:
		frappe.throw(_("At least one parent is required."))
	parents = list(dict.fromkeys(parents))[:300]

	items = []
	for parent in parents:
		try:
			invite_status = get_parent_portal_invite_status(parent)
			if invite_status.get("status") != "never_invited":
				items.append({
					"parent": parent,
					"sent": False,
					"status": "skipped",
					"reason": invite_status.get("reason") or _("Already invited or logged in."),
					"portal_invite_status": invite_status,
				})
				continue
			items.append(_invite_parent_to_portal(parent, source="Bulk Never Invited"))
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), f"QAS parent portal invite failed: {parent}")
			items.append({"parent": parent, "sent": False, "status": "failed", "reason": str(exc)})
	frappe.db.commit()
	return {
		"count": len(items),
		"sent": len([row for row in items if row.get("sent")]),
		"failed": len([row for row in items if row.get("status") == "failed"]),
		"skipped": len([row for row in items if row.get("status") == "skipped"]),
		"items": items,
	}


def get_term_parent_invite_preview_data(term=None, status=None, scope=None):
	_require_school_admin()
	term = _clean_text(term)
	status = _normalise_term_parent_invite_status(status)
	scope = _normalise_term_parent_invite_scope(scope)
	if scope == "term" and not term:
		frappe.throw(_("Term is required."))
	if scope == "term" and not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	return _build_term_parent_invite_preview(term, status, scope)


def start_term_parent_invite_job_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	term = _clean_text(payload.get("term"))
	scope = _normalise_term_parent_invite_scope(payload.get("scope"))
	mode = _normalise_term_parent_invite_status(payload.get("mode") or payload.get("status") or "never_invited")
	parents = _unique_clean_names(payload.get("parents") or [])
	if scope == "term" and not term:
		frappe.throw(_("Term is required."))
	if scope == "term" and not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	if mode not in TERM_PARENT_INVITE_SEND_MODES:
		frappe.throw(_("This invite status cannot be sent in bulk."))
	if not parents:
		frappe.throw(_("At least one parent is required."))
	if len(parents) > TERM_PARENT_INVITE_MAX_RECIPIENTS:
		frappe.throw(_("Parent Portal invite jobs are limited to {0} recipients.").format(TERM_PARENT_INVITE_MAX_RECIPIENTS))

	scope_parent_names = set(_scope_parent_names(term, scope))
	parents = [parent for parent in parents if parent in scope_parent_names]
	if not parents:
		frappe.throw(_("No selected parents match this recipient group."))

	job_id = frappe.generate_hash(length=16)
	status = _term_parent_invite_initial_status(job_id, term, mode, parents, scope)
	_set_term_parent_invite_job_status(job_id, status)
	frappe.enqueue(
		"qas_custom.services.portal_invites.run_term_parent_invite_job",
		queue="long",
		timeout=1800,
		job_name=f"QAS Term Parent Portal Invites {job_id}",
		enqueue_after_commit=True,
		qas_job_id=job_id,
		term=term,
		scope=scope,
		mode=mode,
		parents=parents,
		requested_by=frappe.session.user,
	)
	return status


def get_term_parent_invite_job_data(job_id=None):
	_require_school_admin()
	job_id = _clean_text(job_id)
	if not job_id:
		frappe.throw(_("Job ID is required."))
	status = _get_term_parent_invite_job_status(job_id)
	if not status:
		frappe.throw(_("Parent Portal invite job was not found or has expired."))
	return status


def run_term_parent_invite_job(qas_job_id=None, term=None, mode=None, parents=None, requested_by=None, scope=None):
	job_id = _clean_text(qas_job_id)
	term = _clean_text(term)
	mode = _normalise_term_parent_invite_status(mode)
	scope = _normalise_term_parent_invite_scope(scope)
	parent_names = _unique_clean_names(parents or [])
	if not job_id:
		return
	if requested_by:
		frappe.set_user(requested_by)

	status = _get_term_parent_invite_job_status(job_id) or _term_parent_invite_initial_status(job_id, term, mode, parent_names, scope)
	status.update({"status": "running", "started_at": now_datetime().isoformat(), "current_parent": None})
	_set_term_parent_invite_job_status(job_id, status)
	allowed_statuses = TERM_PARENT_INVITE_STATUS_OPTIONS[mode]["statuses"]

	for parent in parent_names:
		status["current_parent"] = parent
		_set_term_parent_invite_job_status(job_id, status)
		try:
			result_row = _run_one_term_parent_invite(term, mode, parent, allowed_statuses, scope)
			status["results"].append(result_row)
			status["processed"] += 1
			if result_row.get("sent"):
				status["sent"] += 1
			elif result_row.get("skipped"):
				status["skipped"] += 1
			else:
				status["failed"] += 1
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"QAS term parent portal invite failed: {parent}")
			status["processed"] += 1
			status["failed"] += 1
			status["results"].append({
				"parent": parent,
				"sent": False,
				"ok": False,
				"status": "failed",
				"message": str(exc),
			})
		_set_term_parent_invite_job_status(job_id, status)

	status["current_parent"] = None
	status["completed_at"] = now_datetime().isoformat()
	status["status"] = "completed_with_errors" if status.get("failed") else "completed"
	_set_term_parent_invite_job_status(job_id, status)
	return status


def invite_teacher_to_portal_data(teacher=None):
	_require_school_admin()
	if not teacher:
		frappe.throw(_("Teacher is required."))
	result = _invite_teacher_to_portal(teacher, source="Manual")
	frappe.db.commit()
	return result


def bulk_invite_teachers_to_portal_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	teachers = payload.get("teachers") or []
	teachers = [str(teacher).strip() for teacher in teachers if str(teacher or "").strip()]
	if not teachers:
		teachers = frappe.get_all(
			"Teacher",
			filters={"status": ["!=", "Inactive"]},
			pluck="name",
			limit=300,
		)
	teachers = list(dict.fromkeys(teachers))[:300]

	items = []
	for teacher in teachers:
		try:
			invite_status = get_teacher_portal_invite_status(teacher)
			if invite_status.get("status") != "never_invited":
				items.append({
					"teacher": teacher,
					"sent": False,
					"status": "skipped",
					"reason": invite_status.get("reason") or _("Already invited, logged in, or not eligible."),
					"portal_invite_status": invite_status,
				})
				continue
			items.append(_invite_teacher_to_portal(teacher, source="Bulk Never Invited"))
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), f"QAS teacher portal invite failed: {teacher}")
			items.append({"teacher": teacher, "sent": False, "status": "failed", "reason": str(exc)})
	frappe.db.commit()
	return {
		"count": len(items),
		"sent": len([row for row in items if row.get("sent")]),
		"failed": len([row for row in items if row.get("status") == "failed"]),
		"skipped": len([row for row in items if row.get("status") == "skipped"]),
		"items": items,
	}


def _invite_parent_to_portal(parent, source="Manual"):
	parent_doc = frappe.get_doc("Parent", parent)
	parent_name = parent_doc.get("parent_name") or parent_doc.name
	email = _parent_email(parent_doc)
	if not email:
		return {"parent": parent_doc.name, "parent_name": parent_name, "sent": False, "status": "skipped", "reason": "No parent email found."}

	user_name = _ensure_parent_portal_user(parent_doc, email, parent_name)
	invite = _create_invite_token(user_name, email)
	_send_parent_portal_invite_email(parent_name, email, invite["reset_link"], invite["expires_at"], invite["token_record"])
	_log_parent_portal_invite(parent_doc, user_name, email, invite, source)
	_add_parent_comment(parent_doc.name, _("Parent Portal invite sent to {0}.").format(email))
	return {
		"parent": parent_doc.name,
		"parent_name": parent_name,
		"linked_user": user_name,
		"email": email,
		"sent": True,
		"status": "sent",
		"token_record": invite["token_record"],
		"expires_at": invite["expires_at"],
		"portal_invite_status": get_parent_portal_invite_status(parent_doc.name),
	}


def _invite_teacher_to_portal(teacher, source="Manual"):
	teacher_doc = frappe.get_doc("Teacher", teacher)
	teacher_name = teacher_doc.get("teacher_name") or teacher_doc.get("teacher_full_name") or teacher_doc.name
	user_name = (teacher_doc.get("user") or "").strip()
	if not user_name or not frappe.db.exists("User", user_name):
		return {"teacher": teacher_doc.name, "teacher_name": teacher_name, "sent": False, "status": "skipped", "reason": "No linked user found."}

	user_doc = frappe.get_doc("User", user_name)
	email = (user_doc.get("email") or teacher_doc.get("email") or user_name or "").strip().lower()
	if not email:
		return {"teacher": teacher_doc.name, "teacher_name": teacher_name, "linked_user": user_name, "sent": False, "status": "skipped", "reason": "No teacher email found."}
	if not user_doc.get("enabled"):
		return {"teacher": teacher_doc.name, "teacher_name": teacher_name, "linked_user": user_name, "email": email, "sent": False, "status": "skipped", "reason": "Linked user is disabled."}
	if user_doc.get("user_type") != "Website User":
		user_doc.user_type = "Website User"
		user_doc.flags.ignore_permissions = True
		user_doc.save(ignore_permissions=True)

	invite = _create_invite_token(user_name, email, portal=PORTAL_TEACHER)
	_send_teacher_portal_invite_email(teacher_name, email, invite["reset_link"], invite["expires_at"], invite["token_record"])
	_add_teacher_comment(teacher_doc.name, _("{0} to {1}.").format(TEACHER_INVITE_COMMENT_MARKER, email))
	return {
		"teacher": teacher_doc.name,
		"teacher_name": teacher_name,
		"linked_user": user_name,
		"email": email,
		"sent": True,
		"status": "sent",
		"token_record": invite["token_record"],
		"expires_at": invite["expires_at"],
		"portal_invite_status": get_teacher_portal_invite_status(teacher_doc.name),
	}


def _ensure_parent_portal_user(parent_doc, email, parent_name):
	user_name = parent_doc.get("linked_user")
	if user_name and not frappe.db.exists("User", user_name):
		user_name = None
	if not user_name:
		user_name = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	if not user_name:
		user_doc = frappe.new_doc("User")
		user_doc.email = email
		user_doc.first_name = parent_name or email
		user_doc.enabled = 1
		user_doc.user_type = "Website User"
		user_doc.send_welcome_email = 0
		user_doc.flags.ignore_permissions = True
		user_doc.insert(ignore_permissions=True)
		user_name = user_doc.name

	user_doc = frappe.get_doc("User", user_name)
	changed = False
	if not user_doc.get("enabled"):
		user_doc.enabled = 1
		changed = True
	if user_doc.get("user_type") != "Website User":
		user_doc.user_type = "Website User"
		changed = True
	if _ensure_user_role(user_doc, PARENT_PORTAL_ROLE):
		changed = True
	if changed:
		user_doc.flags.ignore_permissions = True
		user_doc.save(ignore_permissions=True)

	if parent_doc.meta.has_field("linked_user") and parent_doc.get("linked_user") != user_name:
		parent_doc.linked_user = user_name
		parent_doc.flags.ignore_permissions = True
		parent_doc.save(ignore_permissions=True)
	return user_name


def get_teacher_portal_invite_status(teacher):
	teacher_doc = frappe.get_doc("Teacher", teacher) if isinstance(teacher, str) else teacher
	teacher_name = teacher_doc.get("teacher_name") or teacher_doc.get("teacher_full_name") or teacher_doc.get("name")
	user_name = (teacher_doc.get("user") or "").strip()
	email = _teacher_email(teacher_doc, user_name)
	login_state = _parent_login_state(user_name)
	history = _teacher_invite_history(teacher_doc.get("name"))

	if login_state.get("has_logged_in"):
		status = "logged_in"
		label = _("Logged in")
		reason = _("Teacher has already logged in.")
	elif history.get("invited"):
		status = "invited_not_logged_in"
		label = _("Invited, not logged in")
		reason = _("Teacher has already been invited.")
	elif not user_name or not frappe.db.exists("User", user_name):
		status = "no_user"
		label = _("No linked user")
		reason = _("Teacher does not have a linked user.")
	elif not email:
		status = "no_email"
		label = _("No email")
		reason = _("No teacher email found.")
	else:
		status = "never_invited"
		label = _("Never invited")
		reason = ""

	return {
		"status": status,
		"label": label,
		"reason": reason,
		"teacher": teacher_doc.get("name"),
		"teacher_name": teacher_name,
		"email": email,
		"linked_user": user_name,
		"invited": bool(history.get("invited")),
		"invite_count": history.get("invite_count") or 0,
		"last_invited_at": history.get("last_invited_at"),
		"last_invite": history.get("last_invite"),
		"has_logged_in": bool(login_state.get("has_logged_in")),
		"last_login": login_state.get("last_login"),
		"last_active": login_state.get("last_active"),
		"bulk_eligible": status == "never_invited",
	}


def _ensure_user_role(user_doc, role):
	if not frappe.db.exists("Role", role):
		frappe.throw(_("Role {0} is required for Parent Portal access.").format(role))
	current_roles = {row.role for row in user_doc.get("roles", []) if row.get("role")}
	if role in current_roles:
		return False
	user_doc.append("roles", {"role": role})
	return True


def get_parent_portal_invite_status(parent):
	parent_doc = frappe.get_doc("Parent", parent) if isinstance(parent, str) else parent
	parent_name = parent_doc.get("parent_name") or parent_doc.get("name")
	email = _parent_email(parent_doc)
	linked_user = _parent_linked_user(parent_doc, email)
	login_state = _parent_login_state(linked_user)
	history = _parent_invite_history(parent_doc.get("name"))

	if login_state.get("has_logged_in"):
		status = "logged_in"
		label = _("Logged in")
		reason = _("Parent has already logged in.")
	elif history.get("invited"):
		status = "invited_not_logged_in"
		label = _("Invited, not logged in")
		reason = _("Parent has already been invited.")
	elif not email:
		status = "no_email"
		label = _("No email")
		reason = _("No parent email found.")
	else:
		status = "never_invited"
		label = _("Never invited")
		reason = ""

	return {
		"status": status,
		"label": label,
		"reason": reason,
		"parent": parent_doc.get("name"),
		"parent_name": parent_name,
		"email": email,
		"linked_user": linked_user,
		"invited": bool(history.get("invited")),
		"invite_count": history.get("invite_count") or 0,
		"last_invited_at": history.get("last_invited_at"),
		"last_invite": history.get("last_invite"),
		"has_logged_in": bool(login_state.get("has_logged_in")),
		"last_login": login_state.get("last_login"),
		"last_active": login_state.get("last_active"),
		"bulk_eligible": status == "never_invited",
	}


def _build_term_parent_invite_preview(term, status_filter, scope="term"):
	scope = _normalise_term_parent_invite_scope(scope)
	if scope == "term":
		enrollment_groups = _term_parent_enrollment_groups(term)
		parent_names = list(enrollment_groups.keys())
		parent_docs = _term_parent_docs(parent_names)
	else:
		parent_docs = _scope_parent_docs(scope)
		parent_names = list(parent_docs.keys())
		enrollment_groups = _all_open_parent_enrollment_groups(parent_names)
	student_labels = _student_label_map(
		{row.get("student") for rows in enrollment_groups.values() for row in rows if row.get("student")}
	)

	rows = []
	for parent in parent_names:
		parent_doc = parent_docs.get(parent) or {"name": parent}
		invite_status = get_parent_portal_invite_status(parent_doc)
		invite_status_key = invite_status.get("status") or "unknown"

		enrollments = enrollment_groups.get(parent) or []
		students = []
		seen_students = set()
		for enrollment in enrollments:
			student = enrollment.get("student")
			if student and student not in seen_students:
				seen_students.add(student)
				students.append({
					"student": student,
					"student_name": student_labels.get(student) or student,
				})

		rows.append({
			"parent": parent,
			"parent_name": invite_status.get("parent_name") or parent_doc.get("parent_name") or parent,
			"email": invite_status.get("email"),
			"linked_user": invite_status.get("linked_user"),
			"parent_status": parent_doc.get("status"),
			"invite_status": invite_status_key,
			"invite_label": invite_status.get("label"),
			"invite_reason": invite_status.get("reason"),
			"invited": invite_status.get("invited"),
			"invite_count": invite_status.get("invite_count") or 0,
			"last_invited_at": invite_status.get("last_invited_at"),
			"last_login": invite_status.get("last_login"),
			"last_active": invite_status.get("last_active"),
			"students": students,
			"student_names": [row["student_name"] for row in students],
			"enrollment_count": len(enrollments),
			"eligible": _term_parent_status_matches(status_filter, invite_status_key) and status_filter in TERM_PARENT_INVITE_SEND_MODES,
		})

	filtered_rows = [
		row for row in rows
		if _term_parent_status_matches(status_filter, row.get("invite_status"))
	]
	filtered_rows.sort(key=lambda row: ((row.get("parent_name") or row.get("parent") or "").lower(), row.get("parent") or ""))

	return {
		"term": term,
		"scope": scope,
		"scope_label": TERM_PARENT_INVITE_SCOPE_OPTIONS[scope]["label"],
		"status": status_filter,
		"status_label": TERM_PARENT_INVITE_STATUS_OPTIONS[status_filter]["label"],
		"total": len(rows),
		"count": len(filtered_rows),
		"eligible_count": len([row for row in filtered_rows if row.get("eligible")]),
		"summary": [
			{
				"status": key,
				"label": option["label"],
				"count": len([row for row in rows if _term_parent_status_matches(key, row.get("invite_status"))]),
				"sendable": key in TERM_PARENT_INVITE_SEND_MODES,
			}
			for key, option in TERM_PARENT_INVITE_STATUS_OPTIONS.items()
		],
		"rows": filtered_rows,
		"send_modes": sorted(TERM_PARENT_INVITE_SEND_MODES),
	}


def _run_one_term_parent_invite(term, mode, parent, allowed_statuses, scope="term"):
	if not frappe.db.exists("Parent", parent):
		return {
			"parent": parent,
			"sent": False,
			"ok": False,
			"status": "failed",
			"message": _("Parent was not found."),
		}
	scope = _normalise_term_parent_invite_scope(scope)
	if scope == "term" and not _parent_has_open_enrollment_in_term(parent, term):
		return {
			"parent": parent,
			"sent": False,
			"ok": True,
			"skipped": True,
			"status": "skipped",
			"message": _("Parent no longer has an open enrollment in this term."),
		}
	if scope == "active_parents" and not _parent_matches_active_scope(parent):
		return {
			"parent": parent,
			"sent": False,
			"ok": True,
			"skipped": True,
			"status": "skipped",
			"message": _("Parent is no longer active."),
		}

	invite_status = get_parent_portal_invite_status(parent)
	if invite_status.get("status") not in allowed_statuses:
		return {
			"parent": parent,
			"parent_name": invite_status.get("parent_name") or parent,
			"email": invite_status.get("email"),
			"sent": False,
			"ok": True,
			"skipped": True,
			"status": "skipped",
			"message": invite_status.get("reason") or _("Parent is no longer eligible for this invite mode."),
			"portal_invite_status": invite_status,
		}

	result = _invite_parent_to_portal(parent, source=_term_parent_invite_source(term, mode, scope))
	return {
		"parent": result.get("parent") or parent,
		"parent_name": result.get("parent_name") or invite_status.get("parent_name"),
		"email": result.get("email") or invite_status.get("email"),
		"sent": bool(result.get("sent")),
		"ok": bool(result.get("sent")),
		"skipped": not result.get("sent"),
		"status": result.get("status"),
		"message": _("Invite sent.") if result.get("sent") else result.get("reason") or _("Invite was not sent."),
		"portal_invite_status": result.get("portal_invite_status"),
	}


def _term_parent_enrollment_groups(term):
	if not _doctype_available("Enrollment"):
		return {}
	fields = _safe_fields("Enrollment", ["name", "parent", "student", "status", "enrollment_type", "course", "term"])
	if "parent" not in fields or "term" not in fields:
		return {}
	rows = frappe.get_all(
		"Enrollment",
		filters={"term": term, "status": ["in", TERM_PARENT_INVITE_OPEN_ENROLLMENT_STATUSES]},
		fields=fields,
		order_by="parent asc, student asc, name asc",
		limit_page_length=0,
	)
	groups = {}
	for row in rows:
		parent = _clean_text(row.get("parent"))
		if not parent:
			continue
		groups.setdefault(parent, []).append(row)
	return groups


def _scope_parent_names(term, scope):
	if scope == "term":
		return list(_term_parent_enrollment_groups(term).keys())
	return list(_scope_parent_docs(scope).keys())


def _scope_parent_docs(scope):
	if not _doctype_available("Parent"):
		return {}
	fields = _safe_fields("Parent", ["name", "parent_name", "email", "email_id", "contact_email", "linked_user", "mobile_number", "phone", "status"])
	filters = {}
	if scope == "active_parents" and "status" in fields:
		filters["status"] = "Active"
	order_by = "parent_name asc, name asc" if "parent_name" in fields else "name asc"
	rows = frappe.get_all(
		"Parent",
		filters=filters,
		fields=fields,
		order_by=order_by,
		limit_page_length=0,
	)
	return {row.get("name"): row for row in rows if row.get("name")}


def _term_parent_docs(parent_names):
	parent_names = _unique_clean_names(parent_names)
	if not parent_names or not _doctype_available("Parent"):
		return {}
	fields = _safe_fields("Parent", ["name", "parent_name", "email", "email_id", "contact_email", "linked_user", "mobile_number", "phone", "status"])
	return {
		row.get("name"): row
		for row in frappe.get_all(
			"Parent",
			filters={"name": ["in", parent_names]},
			fields=fields,
			limit_page_length=0,
		)
	}


def _all_open_parent_enrollment_groups(parent_names):
	parent_names = _unique_clean_names(parent_names)
	if not parent_names or not _doctype_available("Enrollment"):
		return {}
	fields = _safe_fields("Enrollment", ["name", "parent", "student", "status", "enrollment_type", "course", "term"])
	if "parent" not in fields:
		return {}
	rows = frappe.get_all(
		"Enrollment",
		filters={
			"parent": ["in", parent_names],
			"status": ["in", TERM_PARENT_INVITE_OPEN_ENROLLMENT_STATUSES],
		},
		fields=fields,
		order_by="parent asc, student asc, name asc",
		limit_page_length=0,
	)
	groups = {}
	for row in rows:
		parent = _clean_text(row.get("parent"))
		if not parent:
			continue
		groups.setdefault(parent, []).append(row)
	return groups


def _student_label_map(student_names):
	student_names = _unique_clean_names(student_names)
	if not student_names or not _doctype_available("Student"):
		return {}
	fields = _safe_fields("Student", ["name", "student_name", "student_code"])
	rows = frappe.get_all("Student", filters={"name": ["in", student_names]}, fields=fields, limit_page_length=0)
	return {row.get("name"): get_student_display_name(row) or row.get("name") for row in rows}


def _parent_has_open_enrollment_in_term(parent, term):
	if not parent or not term or not _doctype_available("Enrollment"):
		return False
	return bool(frappe.get_all(
		"Enrollment",
		filters={
			"parent": parent,
			"term": term,
			"status": ["in", TERM_PARENT_INVITE_OPEN_ENROLLMENT_STATUSES],
		},
		pluck="name",
		limit=1,
	))


def _parent_matches_active_scope(parent):
	if not parent or not _doctype_available("Parent") or not frappe.db.exists("Parent", parent):
		return False
	fields = _safe_fields("Parent", ["status"])
	if "status" not in fields:
		return True
	return frappe.db.get_value("Parent", parent, "status") == "Active"


def _normalise_term_parent_invite_status(status):
	status = _clean_text(status) or "never_invited"
	if status not in TERM_PARENT_INVITE_STATUS_OPTIONS:
		frappe.throw(_("Unsupported Parent Portal invite status: {0}").format(status))
	return status


def _normalise_term_parent_invite_scope(scope):
	scope = _clean_text(scope) or "term"
	if scope not in TERM_PARENT_INVITE_SCOPE_OPTIONS:
		frappe.throw(_("Unsupported Parent Portal recipient group: {0}").format(scope))
	return scope


def _term_parent_status_matches(filter_status, row_status):
	option = TERM_PARENT_INVITE_STATUS_OPTIONS.get(filter_status)
	return bool(option and row_status in option["statuses"])


def _term_parent_invite_source(term, mode, scope="term"):
	# ``source`` is a Select field. Batch context belongs to the job status,
	# not in this fixed-value audit field.
	return "Bulk Never Invited"


def _term_parent_invite_initial_status(job_id, term, mode, parents, scope="term"):
	scope = _normalise_term_parent_invite_scope(scope)
	return {
		"job_id": job_id,
		"status": "queued",
		"term": term,
		"scope": scope,
		"scope_label": TERM_PARENT_INVITE_SCOPE_OPTIONS[scope]["label"],
		"mode": mode,
		"mode_label": TERM_PARENT_INVITE_STATUS_OPTIONS.get(mode, {}).get("label") or mode,
		"total": len(parents or []),
		"processed": 0,
		"sent": 0,
		"failed": 0,
		"skipped": 0,
		"current_parent": None,
		"results": [],
		"created_at": now_datetime().isoformat(),
		"started_at": None,
		"completed_at": None,
	}


def _term_parent_invite_job_cache_key(job_id):
	return f"qas:school_admin:term_parent_portal_invite:{job_id}"


def _set_term_parent_invite_job_status(job_id, status):
	frappe.cache().set_value(
		_term_parent_invite_job_cache_key(job_id),
		status,
		expires_in_sec=TERM_PARENT_INVITE_JOB_TTL_SECONDS,
	)


def _get_term_parent_invite_job_status(job_id):
	return frappe.cache().get_value(_term_parent_invite_job_cache_key(job_id))


def _unique_clean_names(values):
	names = []
	seen = set()
	for value in values or []:
		name = _clean_text(value)
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def _clean_text(value):
	return str(value or "").strip()


def _parent_linked_user(parent_doc, email=None):
	linked_user = parent_doc.get("linked_user")
	if linked_user and frappe.db.exists("User", linked_user):
		return linked_user
	if email:
		return frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	return ""


def _parent_login_state(user_name):
	if not user_name or not frappe.db.exists("User", user_name):
		return {"has_logged_in": False, "last_login": None, "last_active": None}
	fields = [fieldname for fieldname in ["last_login", "last_active"] if frappe.get_meta("User").has_field(fieldname)]
	if not fields:
		return {"has_logged_in": False, "last_login": None, "last_active": None}
	values = frappe.db.get_value("User", user_name, fields, as_dict=True) or {}
	last_login = values.get("last_login")
	last_active = values.get("last_active")
	return {
		"has_logged_in": bool(last_login or last_active),
		"last_login": str(last_login) if last_login else None,
		"last_active": str(last_active) if last_active else None,
	}


def _parent_invite_history(parent):
	if not parent:
		return {"invited": False, "invite_count": 0}
	log_history = _parent_invite_log_history(parent)
	if log_history.get("invited"):
		return log_history
	comment_history = _parent_invite_comment_history(parent)
	if comment_history.get("invited"):
		return comment_history
	return {"invited": False, "invite_count": 0}


def _parent_invite_log_history(parent):
	if not frappe.db.exists("DocType", INVITE_LOG_DOCTYPE):
		return {"invited": False, "invite_count": 0}
	count = frappe.db.count(INVITE_LOG_DOCTYPE, {"parent": parent})
	if not count:
		return {"invited": False, "invite_count": 0}
	latest = frappe.get_all(
		INVITE_LOG_DOCTYPE,
		filters={"parent": parent},
		fields=["name", "sent_at"],
		order_by="sent_at desc, creation desc",
		limit=1,
	)
	return {
		"invited": True,
		"invite_count": count,
		"last_invite": latest[0].name if latest else None,
		"last_invited_at": str(latest[0].sent_at) if latest and latest[0].sent_at else None,
	}


def _parent_invite_comment_history(parent):
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Parent",
			"reference_name": parent,
			"content": ["like", "%Parent Portal invite sent%"],
		},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {"invited": False, "invite_count": 0}
	return {
		"invited": True,
		"invite_count": 1,
		"last_invite": rows[0].name,
		"last_invited_at": str(rows[0].creation) if rows[0].creation else None,
	}


def _log_parent_portal_invite(parent_doc, user_name, email, invite, source):
	if not frappe.db.exists("DocType", INVITE_LOG_DOCTYPE):
		return
	if source not in PARENT_PORTAL_INVITE_LOG_SOURCES:
		frappe.log_error(
			title="Parent Portal Invite Log Source Normalised",
			message="Unsupported Parent Portal invite source: {0}".format(source),
		)
		source = "Bulk Never Invited"
	frappe.get_doc({
		"doctype": INVITE_LOG_DOCTYPE,
		"parent": parent_doc.name,
		"parent_name": parent_doc.get("parent_name") or parent_doc.name,
		"user": user_name,
		"email": email,
		"sent_at": now_datetime(),
		"source": source or "Manual",
		"status": "Sent",
		"token_record": invite.get("token_record"),
		"expires_at": invite.get("expires_at"),
		"sent_by": frappe.session.user,
	}).insert(ignore_permissions=True)


def _create_invite_token(user_name, email, portal=PORTAL_PARENT):
	if not frappe.db.exists("DocType", PASSWORD_RESET_TOKEN_DOCTYPE):
		frappe.throw(_("Portal password reset tokens are not installed."))
	now = now_datetime()
	expires_at = add_to_date(now, days=PORTAL_INVITE_EXPIRY_DAYS, as_datetime=True)
	reset_token = secrets.token_urlsafe(32)
	token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
	doc = frappe.get_doc({
		"doctype": PASSWORD_RESET_TOKEN_DOCTYPE,
		"user": user_name,
		"email": email,
		"token_hash": token_hash,
		"status": "Pending",
		"requested_at": now,
		"expires_at": expires_at,
		"request_ip": getattr(frappe.local, "request_ip", None),
	})
	doc.insert(ignore_permissions=True)
	return {"token_record": doc.name, "reset_link": _build_password_reset_link(reset_token, portal=portal), "expires_at": expires_at}


def _send_parent_portal_invite_email(parent_name, email, reset_link, expires_at, token_record):
	subject = _("Your QAS Parent Portal: leave, makeup classes and updates")
	greeting = _("Hi {0},").format(parent_name) if parent_name else _("Hi,")
	safe_greeting = escape_html(greeting)
	safe_reset_link = escape_html(reset_link)
	safe_expires_at = escape_html(str(expires_at))
	safe_guide_link = escape_html(PARENT_PORTAL_QUICK_GUIDE_URL)
	message = f"""
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">Queensland Art School</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Your Parent Portal is ready</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{safe_greeting}</p>
						<p style="margin:0 0 16px;font-size:16px;line-height:1.5;">Queensland Art School has created a Parent Portal account for your family.</p>
						<p style="margin:0 0 10px;font-size:16px;line-height:1.5;">You can use the portal to:</p>
						<ul style="margin:0 0 18px 20px;padding:0;font-size:15px;line-height:1.55;color:#172033;">
							<li style="margin:0 0 6px;">request leave when your child cannot attend class</li>
							<li style="margin:0 0 6px;">use leave vouchers to book makeup classes</li>
							<li style="margin:0 0 6px;">check your store credit balance</li>
							<li style="margin:0 0 6px;">see class updates, homework, photos and videos</li>
							<li style="margin:0;">review invoices and payment status when needed</li>
						</ul>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Please use the secure link below to choose your password and sign in.</p>
						<p style="margin:0 0 12px;"><a href="{safe_reset_link}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">Set password</a></p>
						<p style="margin:0 0 22px;"><a href="{safe_guide_link}" style="display:inline-block;background:#eef6ff;color:#2563eb;text-decoration:none;border-radius:10px;padding:11px 16px;font-weight:700;">View Parent Portal quick guide</a></p>
						<p style="margin:0 0 10px;font-size:13px;line-height:1.5;color:#64748b;">This link expires at {safe_expires_at}.</p>
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If the button does not work, copy this link into your browser:<br>{safe_reset_link}</p>
					</div>
				</div>
			</div>
		</div>
	"""
	try:
		result = sendmail_or_skip(
			action="parent_portal_invite",
			recipients=[email],
			subject=subject,
			message=message,
			delayed=False,
		)
		if result and result.get("skipped"):
			return result
	except Exception:
		frappe.log_error(
			title="Parent Portal Invite Email Delivery Failed",
			message=frappe.get_traceback() + f"\n\nToken Record: {token_record}\nRecipient: {email}\nInvite Link: {reset_link}",
		)
		raise


def _send_teacher_portal_invite_email(teacher_name, email, reset_link, expires_at, token_record):
	subject = _("Welcome to Queensland Art School Teacher Portal")
	greeting = _("Hi {0},").format(teacher_name) if teacher_name else _("Hi,")
	safe_greeting = escape_html(greeting)
	safe_reset_link = escape_html(reset_link)
	safe_expires_at = escape_html(str(expires_at))
	message = f"""
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">Queensland Art School</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Set up your Teacher Portal</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{safe_greeting}</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">Queensland Art School has created a Teacher Portal account for you. Please use the secure link below to choose your password and sign in.</p>
						<p style="margin:0 0 22px;"><a href="{safe_reset_link}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">Set password</a></p>
						<p style="margin:0 0 10px;font-size:13px;line-height:1.5;color:#64748b;">This link expires at {safe_expires_at}.</p>
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If the button does not work, copy this link into your browser:<br>{safe_reset_link}</p>
					</div>
				</div>
			</div>
		</div>
	"""
	try:
		result = sendmail_or_skip(
			action="teacher_portal_invite",
			recipients=[email],
			subject=subject,
			message=message,
			delayed=False,
		)
		if result and result.get("skipped"):
			return result
	except Exception:
		frappe.log_error(
			title="Teacher Portal Invite Email Delivery Failed",
			message=frappe.get_traceback() + f"\n\nToken Record: {token_record}\nRecipient: {email}\nInvite Link: {reset_link}",
		)
		raise


def _parent_email(parent_doc):
	for fieldname in ["email", "email_id", "contact_email", "linked_user"]:
		value = parent_doc.get(fieldname)
		if value:
			if fieldname == "linked_user" and frappe.db.exists("User", value):
				return (frappe.db.get_value("User", value, "email") or value or "").strip().lower()
			return str(value).strip().lower()
	return ""


def _teacher_email(teacher_doc, user_name=None):
	for fieldname in ["email", "email_id", "contact_email"]:
		value = teacher_doc.get(fieldname)
		if value:
			return str(value).strip().lower()
	if user_name and frappe.db.exists("User", user_name):
		return (frappe.db.get_value("User", user_name, "email") or user_name or "").strip().lower()
	return ""


def _teacher_invite_history(teacher):
	if not teacher:
		return {"invited": False, "invite_count": 0}
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Teacher",
			"reference_name": teacher,
			"content": ["like", f"%{TEACHER_INVITE_COMMENT_MARKER}%"],
		},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return {"invited": False, "invite_count": 0}
	return {
		"invited": True,
		"invite_count": 1,
		"last_invite": rows[0].name,
		"last_invited_at": str(rows[0].creation) if rows[0].creation else None,
	}


def _add_parent_comment(parent, message):
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "Parent",
		"reference_name": parent,
		"content": message,
	}).insert(ignore_permissions=True)


def _add_teacher_comment(teacher, message):
	frappe.get_doc({
		"doctype": "Comment",
		"comment_type": "Comment",
		"reference_doctype": "Teacher",
		"reference_name": teacher,
		"content": message,
	}).insert(ignore_permissions=True)


def _get_payload(payload):
	if isinstance(payload, str):
		return json.loads(payload or "{}")
	return payload or {}
