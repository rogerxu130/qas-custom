from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import mimetypes
import re
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.sessions import clear_sessions
from frappe.utils import add_days, cint, flt, get_time, getdate, now_datetime, nowdate, today, validate_email_address

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.modules.attendance.commands import create_full_term_attendance_entries, update_attendance_status
from qas_custom.modules.billing.store_credit import (
	adjust_store_credit,
	apply_store_credit_to_invoice,
	apply_store_credit_to_unpaid_invoices,
	cancel_store_credit_journal_entries,
	create_store_credit_entry,
	get_store_credit_bonus_for_source,
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
	get_invoice_total_amount,
	get_store_credit_summary,
	grant_store_credit_bonus_for_amount,
	grant_store_credit_bonus_for_payment_entry,
	has_invoice_store_credit_journal_entry,
	sync_invoice_store_credit_snapshot,
)
from qas_custom.modules.billing.invoice_settings import (
	SNAPSHOT_FIELD_MAP,
	apply_default_invoice_dates,
	apply_invoice_payment_snapshot,
	get_invoice_settings,
	update_invoice_settings,
)
from qas_custom.modules.billing.commands import (
	get_course_money,
	get_course_number,
	get_invoice_customer,
	get_invoice_item,
)
from qas_custom.modules.billing.presentation import build_course_invoice_description, invoice_item_schedule
from qas_custom.modules.makeup.commands import (
    cancel_makeup_booking_core,
    get_parent_redeemable_sessions_core,
    redeem_parent_voucher_core,
    submit_parent_leave_request_core,
)
from qas_custom.modules.notifications import (
	enqueue_parent_invoice_cancellation_notification,
	enqueue_parent_invoice_paid_receipt,
	enqueue_parent_invoice_notification,
	get_invoice_notification_summary,
	maybe_send_parent_invoice_paid_receipt,
	parent_portal_invoice_link,
	send_parent_invoice_notification,
)
from qas_custom.modules.notifications.guard import disable_sales_invoice_auto_notifications
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, create_attendance_entry, get_attendance_entries
from qas_custom.services.display_labels import get_course_session_snapshot_label, get_makeup_voucher_label, get_student_display_code, get_student_display_name, get_student_parent_name
from qas_custom.utils.environment import payment_block_reason, payment_mutations_enabled
from qas_custom.services.inquiry import (
	add_inquiry_note_core,
	build_inquiry_detail,
	build_inquiry_summary,
	create_inquiry_core,
	mark_inquiry_status_core,
	reschedule_inquiry_core,
	send_trial_class_reminder_core,
	update_inquiry_confirmation_core,
)
from qas_custom.services.teacher_revenue_share import get_teacher_revenue_share_session_rows
from qas_custom.services.teacher_directory import get_active_teacher_directory_data


ADMIN_ROLES = {"School Admin", "System Manager"}
INQUIRY_OPEN_STATUSES = ["New", "Needs Review", "Booked", "Rescheduled", "No-show"]
INQUIRY_POST_VISIT_STATUSES = ["Completed", "Follow-up"]
INQUIRY_STATUSES = {
	"New",
	"Needs Review",
	"Booked",
	"Rescheduled",
	"Cancelled",
	"Completed",
	"No-show",
	"Follow-up",
	"Converted",
	"Inactive",
}
ACTIVE_TERM_STATUSES = ["Upcoming", "Active"]
ACTIVE_TIMESLOT_STATUSES = ["Active"]
COURSE_LABEL_FIELDS = ["name", "course_name", "course_name_zh"]
DEFAULT_COURSE_INVOICE_ITEM = "Tuition Fee"
MANUAL_INVOICE_ITEM = "Other"
BULK_INVOICE_SUBMIT_JOB_TTL_SECONDS = 86400
NON_ATTENDING_ATTENDANCE_STATUSES = {"Cancelled", "Leave"}
TRIAL_CONFIRMATION_STATUSES = {"Pending", "Text Message Sent", "Customer Confirmed"}
SCHOOL_ADMIN_LEAVE_ATTENDANCE_STATUSES = ("To be started", "Absent")
TRANSFER_CANCELLABLE_ATTENDANCE_STATUSES = {"To be started", "Scheduled"}
TRANSFER_RETAINED_ATTENDANCE_STATUSES = {"Present", "Absent", "Late", "Leave"}
PARENT_EDIT_FIELDS = ["parent_name", "mobile_number", "phone", "email", "email_id", "address", "status", "customer"]
PARENT_UPDATE_FIELDS = ["parent_name", "mobile_number", "phone", "address", "status", "customer"]
PARENT_EMAIL_FIELDS = ("email", "email_id", "contact_email")
PARENT_PORTAL_INVITE_LOG_DOCTYPE = "Parent Portal Invite Log"
PARENT_PORTAL_RESET_TOKEN_DOCTYPE = "Portal Password Reset Token"
STUDENT_EDIT_FIELDS = ["student_name", "first_name", "last_name", "date_of_birth", "dob", "gender", "status", "guardian", "parent", "teaching_notes"]
COURSE_EDIT_FIELDS = [
	"course_name",
	"course_name_zh",
	"status",
	"duration_mins",
	"min_age",
	"max_age",
	"invoice_item",
	"full_term_fee",
	"pay_as_you_go_fee",
	"total_session_per_term",
	"term_session_fee",
	"is_makeup_course",
]
SCHOOL_SETUP_TYPES = {
	"campus": {
		"doctype": "Campus",
		"title_field": "campus_name",
		"fields": ["name", "campus_name", "status", "email", "phone", "address", "notes", "modified"],
		"edit_fields": ["campus_name", "status", "email", "phone", "address", "notes"],
		"required": ["campus_name"],
		"search_fields": ["name", "campus_name", "email", "phone", "address"],
		"order_by": "campus_name asc",
	},
	"classroom": {
		"doctype": "Classroom",
		"title_field": "classroom_name",
		"fields": ["name", "classroom_name", "campus", "status", "capacity", "notes", "modified"],
		"edit_fields": ["classroom_name", "campus", "status", "capacity", "notes"],
		"required": ["classroom_name", "campus"],
		"search_fields": ["name", "classroom_name", "campus", "notes"],
		"order_by": "campus asc, classroom_name asc",
	},
	"teacher": {
		"doctype": "Teacher",
		"title_field": "teacher_name",
		"fields": ["name", "teacher_name", "user", "status", "email", "mobile", "phone", "notes", "modified"],
		"edit_fields": ["teacher_name", "user", "status", "email", "mobile", "phone", "notes"],
		"required": ["teacher_name"],
		"search_fields": ["name", "teacher_name", "user", "email", "mobile", "phone"],
		"order_by": "teacher_name asc",
	},
}


def get_school_admin_me_data():
	_require_school_admin()
	return {
		"user": frappe.session.user,
		"roles": sorted(set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES)),
		"active": True,
	}


def get_school_admin_csrf_token_data():
	_require_school_admin()
	return {"csrf_token": frappe.sessions.get_csrf_token()}


def get_school_admin_teacher_directory_data(query=None, limit=300):
	_require_school_admin()
	return get_active_teacher_directory_data(query=query, limit=limit)


def get_school_admin_dashboard_data():
	_require_school_admin()
	from qas_custom.services.payment_collection_requests import get_pending_payment_request_count

	start_date = getdate(today())
	end_date = getdate(add_days(start_date, 7))
	outstanding = _get_outstanding_invoice_summary()
	draft_invoice_filters = _draft_invoice_submit_filters()
	active_enrollment_filters = {"status": "Active"}
	_apply_active_term_filter(active_enrollment_filters)
	return {
		"date": str(start_date),
		"action_counts": {
			"draft_invoices": _count_sales_invoices(draft_invoice_filters),
			"pending_payment_requests": get_pending_payment_request_count(),
			"trial_needs_scheduling": _count(
				"Inquiry",
				{"inquiry_type": "Trial Lesson", "status": "Needs Review"},
			),
			"school_visit_needs_review": _count(
				"Inquiry",
				{"inquiry_type": "School Visit", "status": "Needs Review"},
			),
			"post_visit_follow_up": _count(
				"Inquiry",
				{"status": ["in", INQUIRY_POST_VISIT_STATUSES]},
			),
			"active_enrollments": _count("Enrollment", active_enrollment_filters),
		},
		"upcoming": {
			"from_date": str(start_date),
			"to_date": str(end_date),
			"trial_lessons": _count(
				"Inquiry",
				{
					"inquiry_type": "Trial Lesson",
					"status": ["in", ["Booked", "Rescheduled"]],
					"current_appointment_date": ["between", [start_date, end_date]],
				},
			),
			"school_visits": _count(
				"Inquiry",
				{
					"inquiry_type": "School Visit",
					"status": ["in", ["Booked", "Rescheduled"]],
					"current_appointment_date": ["between", [start_date, end_date]],
				},
			),
			"course_sessions": len(
				_get_course_session_rows(
					from_date=start_date,
					to_date=end_date,
					limit=500,
				)
			),
		},
		"financial": {
			"submitted_invoices": _count_sales_invoices({"docstatus": 1}),
			"cancelled_invoices": _count_sales_invoices({"docstatus": 2}),
			"outstanding_invoice_count": outstanding.get("count"),
			"outstanding_amount": outstanding.get("amount"),
		},
	}


def school_admin_global_search_data(query=None, limit=20):
	_require_school_admin()
	query = (query or "").strip()
	if len(query) < 2:
		frappe.throw(_("Search query must be at least 2 characters."))
	limit = _limit(limit, default=20, max_value=50)
	return {
		"query": query,
		"families": _search_parents(query, limit),
		"students": _search_students(query, limit),
		"customers": _search_customers(query, limit),
		"inquiries": _search_inquiries(query, limit),
		"enrollments": _search_enrollments(query, limit),
		"invoices": _search_invoices(query, limit),
	}


def get_school_admin_family_data(parent=None, student=None, customer=None, email=None):
	_require_school_admin()
	context = _resolve_family_context(parent=parent, student=student, customer=customer, email=email)
	if not context.get("parent") and not context.get("student") and not context.get("customer"):
		frappe.throw(_("Family was not found."))

	parent_id = context.get("parent")
	student_id = context.get("student")
	customer_id = context.get("customer")
	students = _get_family_students(parent_id, student_id)
	student_ids = [row.get("name") for row in students if row.get("name")]
	return {
		"parent": _get_parent_payload(parent_id) if parent_id else None,
		"customer": _get_customer_payload(customer_id) if customer_id else None,
		"students": students,
		"store_credit": get_store_credit_summary(parent=parent_id, customer=customer_id, limit=20) if customer_id else None,
		"enrollments": _get_enrollment_rows(parent=parent_id, students=student_ids, limit=80),
		"inquiries": _get_family_inquiry_rows(parent=parent_id, students=student_ids, email=email, limit=80),
		"invoices": _get_invoice_rows(customer=customer_id, parent=parent_id, students=student_ids, limit=80),
		"vouchers": _get_family_voucher_rows(students=student_ids, limit=80),
	}




def get_school_admin_parents_data(query=None, status=None, invite_status=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Parent"):
		return {"items": []}
	filters = {}
	if status and _has_field("Parent", "status"):
		filters["status"] = status
	fields = _safe_fields("Parent", ["name", *PARENT_EDIT_FIELDS, "linked_user", "modified"])
	or_filters = _text_search_filters("Parent", query, ["name", "parent_name", "mobile_number", "phone", "email", "email_id"])
	student_parent_ids = _matching_student_parent_ids(query)
	if student_parent_ids:
		or_filters = or_filters or []
		or_filters.append(["Parent", "name", "in", student_parent_ids])
	rows = frappe.get_all(
		"Parent",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit=_limit(limit, default=120, max_value=300),
	)
	items = _attach_parent_portal_invite_statuses([_normalize_row_payload("Parent", row) for row in rows])
	if invite_status:
		items = [row for row in items if (row.get("portal_invite_status") or {}).get("status") == invite_status]
	return {"items": items[: _limit(limit, default=120, max_value=300)]}


def create_school_admin_parent_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	email = _parent_payload_email(payload)
	linked_user = _get_or_create_school_admin_parent_user(email, payload.get("parent_name") or payload.get("name")) if email else None
	if linked_user and _has_field("Parent", "linked_user"):
		existing_parent = frappe.db.get_value("Parent", {"linked_user": linked_user}, "name")
		if existing_parent:
			return _get_parent_payload(existing_parent)

	doc = frappe.new_doc("Parent")
	_apply_master_payload(doc, payload, PARENT_EDIT_FIELDS)
	if linked_user:
		_set_if_field(doc, "linked_user", linked_user)
		_set_parent_email_fields(doc, email)
	if not doc.get("parent_name") and payload.get("name"):
		_set_if_field(doc, "parent_name", payload.get("name"))
	if _has_field("Parent", "status") and not doc.get("status"):
		_set_if_field(doc, "status", "Active")
	_validate_required(doc, ["parent_name"])
	doc.insert(ignore_permissions=True)
	_add_comment("Parent", doc.name, _("Parent created by School Admin."))
	frappe.db.commit()
	return _get_parent_payload(doc.name)


def _parent_payload_email(payload):
	for fieldname in ["email", "email_id", "contact_email", "linked_user"]:
		value = (payload.get(fieldname) or "").strip().lower()
		if value:
			return value
	return ""


def _get_or_create_school_admin_parent_user(email: str | None, parent_name: str | None):
	if not email:
		return None
	user = frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")
	if user:
		return user

	user_doc = frappe.new_doc("User")
	user_doc.email = email
	user_doc.first_name = parent_name or email
	user_doc.enabled = 1
	user_doc.user_type = "Website User"
	user_doc.send_welcome_email = 0
	if frappe.db.exists("Role", "Parent"):
		user_doc.append("roles", {"role": "Parent"})
	user_doc.flags.ignore_permissions = True
	user_doc.insert(ignore_permissions=True)
	return user_doc.name


def _set_parent_email_fields(doc, email):
	for fieldname in ["email", "email_id", "contact_email"]:
		_set_if_field(doc, fieldname, email)


def update_school_admin_parent_data(parent=None, payload=None):
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	doc = frappe.get_doc("Parent", parent)
	_apply_master_payload(doc, _get_payload(payload), PARENT_UPDATE_FIELDS)
	_validate_required(doc, ["parent_name"])
	doc.save(ignore_permissions=True)
	_add_comment("Parent", doc.name, _("Parent updated by School Admin."))
	frappe.db.commit()
	return get_school_admin_family_data(parent=doc.name)


def correct_school_admin_parent_email_data(parent=None, email=None):
	"""Replace a Parent's email identity without reusing the old portal User."""
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	new_email = _normalise_parent_email(email)
	parent_doc = frappe.get_doc("Parent", parent)
	old_email = _parent_current_email(parent_doc)
	if new_email == old_email:
		frappe.throw(_("The corrected email must be different from the current email."))
	_conflicting_parent_email_identity(parent_doc, new_email)
	old_user = parent_doc.get("linked_user") if _has_field("Parent", "linked_user") else None
	if old_user and not frappe.db.exists("User", old_user):
		old_user = None
	try:
		_set_parent_email_fields(parent_doc, new_email)
		_update_parent_customer_email(parent_doc, new_email)
		_update_parent_primary_contact_email(parent_doc, new_email)
		if old_user:
			_revoke_parent_portal_user(old_user)
		_invalidate_parent_portal_invites(parent_doc.name)
		_set_if_field(parent_doc, "linked_user", _create_replacement_parent_portal_user(new_email, parent_doc.get("parent_name") or parent_doc.name))
		parent_doc.save(ignore_permissions=True)
		_add_comment("Parent", parent_doc.name, _("Parent email corrected from {0} to {1} by {2}. The previous Parent Portal login and invitations were revoked.").format(old_email or _("(none)"), new_email, frappe.session.user))
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	return get_school_admin_family_data(parent=parent_doc.name)


def _normalise_parent_email(email):
	email = str(email or "").strip().lower()
	if not email:
		frappe.throw(_("A corrected email is required."))
	try:
		validate_email_address(email, throw=True)
	except Exception:
		frappe.throw(_("Enter a valid email address."))
	return email


def _parent_current_email(parent_doc):
	for fieldname in PARENT_EMAIL_FIELDS:
		if parent_doc.get(fieldname):
			return str(parent_doc.get(fieldname)).strip().lower()
	linked_user = parent_doc.get("linked_user")
	if linked_user and frappe.db.exists("User", linked_user):
		return (frappe.db.get_value("User", linked_user, "email") or "").strip().lower()
	return ""


def _conflicting_parent_email_identity(parent_doc, email):
	contact_names = set(_parent_customer_contact_names(parent_doc))
	for doctype, fields in (("Parent", PARENT_EMAIL_FIELDS), ("Customer", PARENT_EMAIL_FIELDS), ("Contact", PARENT_EMAIL_FIELDS)):
		if not _doctype_available(doctype):
			continue
		for fieldname in fields:
			if not _has_field(doctype, fieldname):
				continue
			for name in frappe.get_all(doctype, filters={fieldname: email}, pluck="name", limit_page_length=2):
				if _is_parent_email_identity_conflict(doctype, name, parent_doc, contact_names):
					frappe.throw(_("The corrected email is already used by {0} {1}. Resolve that record before changing this family.").format(doctype, name))
	if frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name"):
		frappe.throw(_("The corrected email already has a User account. Resolve that account before changing this family."))


def _is_parent_email_identity_conflict(doctype, name, parent_doc, contact_names):
	"""The family owns its Parent, Customer, and Customer-linked Contact records."""
	if doctype == "Parent":
		return name != parent_doc.name
	if doctype == "Customer":
		return name != parent_doc.get("customer")
	if doctype == "Contact":
		return name not in contact_names
	return True


def _parent_customer_contact_names(parent_doc):
	customer = parent_doc.get("customer")
	if not customer or not _doctype_available("Dynamic Link"):
		return []
	return frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		pluck="parent",
		limit_page_length=20,
	)


def _update_parent_customer_email(parent_doc, email):
	customer = parent_doc.get("customer")
	if not customer or not _doctype_available("Customer") or not frappe.db.exists("Customer", customer):
		return
	doc = frappe.get_doc("Customer", customer)
	changed = False
	for fieldname in PARENT_EMAIL_FIELDS:
		if doc.meta.has_field(fieldname) and doc.get(fieldname) != email:
			doc.set(fieldname, email)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def _update_parent_primary_contact_email(parent_doc, email):
	customer = parent_doc.get("customer")
	if not customer or not _doctype_available("Contact") or not _doctype_available("Dynamic Link"):
		return
	names = _parent_customer_contact_names(parent_doc)
	if not names:
		return
	order_by = "is_primary_contact desc, modified desc" if _has_field("Contact", "is_primary_contact") else "modified desc"
	contacts = frappe.get_all("Contact", filters={"name": ["in", names]}, fields=_safe_fields("Contact", ["name", "is_primary_contact", "modified"]), order_by=order_by, limit_page_length=20)
	if not contacts:
		return
	doc = frappe.get_doc("Contact", contacts[0].name)
	changed = False
	for fieldname in PARENT_EMAIL_FIELDS:
		if doc.meta.has_field(fieldname) and doc.get(fieldname) != email:
			doc.set(fieldname, email)
			changed = True
	if doc.meta.has_field("email_ids"):
		rows = doc.get("email_ids") or []
		row = next((item for item in rows if item.get("is_primary")), rows[0] if rows else None)
		if row and row.get("email_id") != email:
			row.email_id = email
			changed = True
	if changed:
		doc.save(ignore_permissions=True)


def _revoke_parent_portal_user(user_name):
	doc = frappe.get_doc("User", user_name)
	if doc.get("enabled"):
		doc.enabled = 0
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
	clear_sessions(user=user_name, force=True)
	if _doctype_available(PARENT_PORTAL_RESET_TOKEN_DOCTYPE):
		frappe.db.set_value(PARENT_PORTAL_RESET_TOKEN_DOCTYPE, {"user": user_name, "status": "Pending"}, "status", "Revoked", update_modified=False)


def _invalidate_parent_portal_invites(parent):
	if _doctype_available(PARENT_PORTAL_INVITE_LOG_DOCTYPE):
		frappe.db.set_value(PARENT_PORTAL_INVITE_LOG_DOCTYPE, {"parent": parent, "status": "Sent"}, "status", "Invalidated", update_modified=False)


def _create_replacement_parent_portal_user(email, parent_name):
	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = parent_name or email
	doc.enabled = 1
	doc.user_type = "Website User"
	doc.send_welcome_email = 0
	if frappe.db.exists("Role", "Parent"):
		doc.append("roles", {"role": "Parent"})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def set_school_admin_parent_status_data(parent=None, status=None):
	_require_school_admin()
	if not parent or not status:
		frappe.throw(_("Parent and status are required."))
	if not _has_field("Parent", "status"):
		frappe.throw(_("Parent status is not available on this site."))
	doc = frappe.get_doc("Parent", parent)
	doc.status = status
	doc.save(ignore_permissions=True)
	_add_comment("Parent", doc.name, _("Parent status changed to {0} by School Admin.").format(status))
	frappe.db.commit()
	return get_school_admin_family_data(parent=doc.name)


def delete_school_admin_parent_data(parent=None):
	_require_school_admin()
	if not parent:
		frappe.throw(_("Parent is required."))
	_assert_safe_delete("Parent", parent)
	frappe.delete_doc("Parent", parent, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": parent}


def get_school_admin_students_data(parent=None, query=None, status=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Student"):
		return {"items": []}
	filters = {}
	parent_field = _student_parent_field()
	if parent and parent_field:
		filters[parent_field] = parent
	if status and _has_field("Student", "status"):
		filters["status"] = status
	fields = _safe_fields("Student", ["name", *STUDENT_EDIT_FIELDS, "age", "student_code", "modified"])
	or_filters = _text_search_filters("Student", query, ["name", "student_name", "first_name", "last_name"])
	rows = frappe.get_all(
		"Student",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit=_limit(limit, default=120, max_value=300),
	)
	return {"items": [_normalize_row_payload("Student", row) for row in rows]}


def create_school_admin_student_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	_normalize_student_teaching_notes(payload)
	doc = frappe.new_doc("Student")
	_apply_master_payload(doc, payload, STUDENT_EDIT_FIELDS)
	if payload.get("parent"):
		_set_student_parent(doc, payload.get("parent"))
	if _has_field("Student", "status") and not doc.get("status"):
		_set_if_field(doc, "status", "Active")
	_validate_required(doc, ["student_name"])
	doc.insert(ignore_permissions=True)
	_add_comment("Student", doc.name, _("Student created by School Admin."))
	frappe.db.commit()
	return get_school_admin_family_data(student=doc.name)


def update_school_admin_student_data(student=None, payload=None):
	_require_school_admin()
	if not student:
		frappe.throw(_("Student is required."))
	payload = _get_payload(payload)
	_normalize_student_teaching_notes(payload)
	doc = frappe.get_doc("Student", student)
	_apply_master_payload(doc, payload, STUDENT_EDIT_FIELDS)
	if payload.get("parent"):
		_set_student_parent(doc, payload.get("parent"))
	_validate_required(doc, ["student_name"])
	doc.save(ignore_permissions=True)
	_add_comment("Student", doc.name, _("Student updated by School Admin."))
	frappe.db.commit()
	return get_school_admin_family_data(student=doc.name)


def set_school_admin_student_status_data(student=None, status=None):
	_require_school_admin()
	if not student or not status:
		frappe.throw(_("Student and status are required."))
	if not _has_field("Student", "status"):
		frappe.throw(_("Student status is not available on this site."))
	doc = frappe.get_doc("Student", student)
	doc.status = status
	doc.save(ignore_permissions=True)
	_add_comment("Student", doc.name, _("Student status changed to {0} by School Admin.").format(status))
	frappe.db.commit()
	return get_school_admin_family_data(student=doc.name)


def delete_school_admin_student_data(student=None):
	_require_school_admin()
	if not student:
		frappe.throw(_("Student is required."))
	_assert_safe_delete("Student", student)
	frappe.delete_doc("Student", student, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": student}


def get_school_admin_courses_data(query=None, status=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Course"):
		return {"items": []}
	filters = {}
	if status and _has_field("Course", "status"):
		filters["status"] = status
	fields = _safe_fields("Course", ["name", *COURSE_EDIT_FIELDS, "modified"])
	or_filters = _text_search_filters("Course", query, ["name", "course_name", "course_name_zh"])
	rows = frappe.get_all(
		"Course",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="name asc",
		limit=_limit(limit, default=120, max_value=500),
	)
	items = [_normalize_row_payload("Course", row) for row in rows]
	for item in items:
		_attach_course_label(item, item.get("name"), item)
	return {"items": items}


def get_school_admin_invoice_items_data(query=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Item"):
		return {"items": []}
	filters = {}
	if _has_field("Item", "disabled"):
		filters["disabled"] = 0
	fields = _safe_fields("Item", ["name", "item_code", "item_name", "item_group", "disabled"])
	rows = frappe.get_all(
		"Item",
		filters=filters,
		or_filters=_text_search_filters("Item", query, ["name", "item_code", "item_name"]),
		fields=fields,
		order_by="name asc",
		limit=_limit(limit, default=120, max_value=500),
		ignore_permissions=True,
	)
	items = [{
		"value": MANUAL_INVOICE_ITEM,
		"label": MANUAL_INVOICE_ITEM,
		"name": MANUAL_INVOICE_ITEM,
		"item_code": MANUAL_INVOICE_ITEM,
		"item_name": MANUAL_INVOICE_ITEM,
		"doctype": "Item",
	}]
	for row in rows:
		item = _normalize_row_payload("Item", row)
		value = item.get("name") or item.get("item_code")
		if value == MANUAL_INVOICE_ITEM:
			continue
		label = item.get("item_name") or item.get("item_code") or value
		if item.get("item_code") and item.get("item_code") != label:
			label = f"{label} · {item.get('item_code')}"
		items.append({"value": value, "label": label, **item})
	return {"items": items}


def create_school_admin_course_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	doc = frappe.new_doc("Course")
	_apply_master_payload(doc, payload, COURSE_EDIT_FIELDS)
	if not doc.get("course_name") and payload.get("name"):
		_set_if_field(doc, "course_name", payload.get("name"))
	if _has_field("Course", "status") and not doc.get("status"):
		_set_if_field(doc, "status", "Active")
	_apply_course_pricing_defaults(doc)
	_apply_course_invoice_item_default(doc)
	_validate_required(doc, ["course_name"])
	doc.insert(ignore_permissions=True)
	_add_comment("Course", doc.name, _("Course created by School Admin."))
	frappe.db.commit()
	return _get_course_payload(doc.name)


def update_school_admin_course_data(course=None, payload=None):
	_require_school_admin()
	if not course:
		frappe.throw(_("Course is required."))
	doc = frappe.get_doc("Course", course)
	payload = _get_payload(payload)
	_apply_master_payload(doc, payload, COURSE_EDIT_FIELDS)
	_apply_course_pricing_defaults(doc)
	_apply_course_invoice_item_default(doc)
	_validate_required(doc, ["course_name"])
	doc.save(ignore_permissions=True)
	_add_comment("Course", doc.name, _("Course updated by School Admin."))
	frappe.db.commit()
	return _get_course_payload(doc.name)


def _apply_course_pricing_defaults(doc):
	if not _has_field("Course", "term_session_fee"):
		return
	full_term_fee_value = doc.get("full_term_fee")
	total_sessions_value = doc.get("total_session_per_term")
	if full_term_fee_value in (None, ""):
		frappe.throw(_("Full term fee is required to calculate term session fee."))
	full_term_fee = flt(full_term_fee_value)
	total_sessions = flt(total_sessions_value)
	if total_sessions <= 0:
		frappe.throw(_("Total sessions per term is required to calculate term session fee."))
	_set_if_field(doc, "term_session_fee", round(full_term_fee / total_sessions, 2))


def _apply_course_invoice_item_default(doc):
	if not _has_field("Course", "invoice_item"):
		return
	item_code = str(doc.get("invoice_item") or DEFAULT_COURSE_INVOICE_ITEM).strip()
	_set_if_field(doc, "invoice_item", _ensure_school_admin_invoice_item(item_code))


def _ensure_school_admin_invoice_item(item_code):
	item_code = str(item_code or "").strip()
	if not item_code:
		frappe.throw(_("Invoice item is required."))
	if frappe.db.exists("Item", item_code):
		return item_code
	if not _doctype_available("Item"):
		frappe.throw(_("Item is not installed on this site."))
	doc = frappe.new_doc("Item")
	_set_if_field(doc, "item_code", item_code)
	_set_if_field(doc, "item_name", item_code)
	_set_if_field(doc, "item_group", _default_school_admin_item_group())
	_set_if_field(doc, "stock_uom", _default_school_admin_stock_uom())
	_set_if_field(doc, "is_stock_item", 0)
	_set_if_field(doc, "disabled", 0)
	doc.insert(ignore_permissions=True)
	return doc.name


def _default_school_admin_item_group():
	if _doctype_available("Item Group") and frappe.db.exists("Item Group", "Services"):
		return "Services"
	filters = {"is_group": 0} if _has_field("Item Group", "is_group") else {}
	rows = frappe.get_all("Item Group", filters=filters, pluck="name", order_by="name asc", limit=1) if _doctype_available("Item Group") else []
	if rows:
		return rows[0]
	frappe.throw(_("Create an Item Group before creating invoice items."))


def _default_school_admin_stock_uom():
	if _doctype_available("UOM") and frappe.db.exists("UOM", "Nos"):
		return "Nos"
	filters = {"enabled": 1} if _has_field("UOM", "enabled") else {}
	rows = frappe.get_all("UOM", filters=filters, pluck="name", order_by="name asc", limit=1) if _doctype_available("UOM") else []
	if rows:
		return rows[0]
	frappe.throw(_("Create a UOM before creating invoice items."))


def set_school_admin_course_status_data(course=None, status=None):
	_require_school_admin()
	if not course or not status:
		frappe.throw(_("Course and status are required."))
	if not _has_field("Course", "status"):
		frappe.throw(_("Course status is not available on this site."))
	doc = frappe.get_doc("Course", course)
	doc.status = status
	doc.save(ignore_permissions=True)
	_add_comment("Course", doc.name, _("Course status changed to {0} by School Admin.").format(status))
	frappe.db.commit()
	return _get_course_payload(doc.name)


def delete_school_admin_course_data(course=None):
	_require_school_admin()
	if not course:
		frappe.throw(_("Course is required."))
	_assert_safe_delete("Course", course)
	frappe.delete_doc("Course", course, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": course}


def get_school_admin_store_credit_data(parent=None, customer=None, limit=50):
	_require_school_admin()
	return get_store_credit_summary(parent=parent, customer=customer, limit=_limit(limit, default=50, max_value=200))


def get_school_admin_invoice_settings_data():
	_require_school_admin()
	return get_invoice_settings()


def update_school_admin_invoice_settings_data(payload=None):
	_require_school_admin()
	settings = update_invoice_settings(_get_payload(payload))
	frappe.db.commit()
	return settings


def get_school_admin_setup_records_data(record_type=None, query=None, status=None, limit=120):
	_require_school_admin()
	config = _school_setup_config(record_type)
	doctype = config["doctype"]
	if not _doctype_available(doctype):
		return {"items": []}
	filters = {}
	if status and _has_field(doctype, "status"):
		filters["status"] = status
	fields = _safe_fields(doctype, config["fields"])
	or_filters = _text_search_filters(doctype, query, config["search_fields"])
	rows = frappe.get_all(
		doctype,
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by=config["order_by"],
		limit=_limit(limit, default=120, max_value=500),
	)
	return {"items": [_school_setup_payload(config, row) for row in rows]}


def save_school_admin_setup_record_data(record_type=None, name=None, payload=None):
	_require_school_admin()
	config = _school_setup_config(record_type)
	doctype = config["doctype"]
	if not _doctype_available(doctype):
		frappe.throw(_("{0} is not installed yet.").format(doctype))
	payload = _get_payload(payload)
	if name:
		doc = frappe.get_doc(doctype, name)
	else:
		doc = frappe.new_doc(doctype)
	_apply_master_payload(doc, payload, config["edit_fields"])
	_normalize_school_setup_record(doc, config)
	_validate_required(doc, config["required"])
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _school_setup_payload(config, doc.as_dict())


def delete_school_admin_setup_record_data(record_type=None, name=None):
	_require_school_admin()
	config = _school_setup_config(record_type)
	doctype = config["doctype"]
	if not name:
		frappe.throw(_("{0} is required.").format(doctype))
	_assert_safe_delete(doctype, name)
	frappe.delete_doc(doctype, name, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "name": name}


def adjust_school_admin_store_credit_data(parent=None, customer=None, amount=0, reason=None, notes=None):
	_require_school_admin()
	amount = flt(amount)
	entry = adjust_store_credit(
		parent=parent,
		customer=customer,
		amount=amount,
		reason=reason,
		notes=notes,
	)
	promotion_bonus = None
	auto_application = None
	if amount > 0:
		promotion_bonus = grant_store_credit_bonus_for_amount(
			parent=entry.parent,
			customer=entry.customer,
			amount=amount,
			scope="Top-up",
			source_doctype="QAS Store Credit Ledger",
			source_document=entry.name,
		)
		auto_application = apply_store_credit_to_unpaid_invoices(parent=entry.parent, customer=entry.customer)
	frappe.db.commit()
	if auto_application and auto_application.get("invoices"):
		for application in auto_application.get("invoices"):
			invoice_name = application.get("invoice")
			if not invoice_name:
				continue
			receipt = _maybe_send_paid_receipt(
				frappe.get_doc("Sales Invoice", invoice_name),
				source="store_credit_adjustment",
			)
			application["receipt_notification"] = receipt
		frappe.db.commit()
	return {
		"entry": entry.as_dict(),
		"promotion_bonus": promotion_bonus,
		"store_credit": get_store_credit_summary(parent=entry.parent, customer=entry.customer, limit=50),
		"auto_application": auto_application,
	}


def get_school_admin_inquiries_data(
	status=None,
	inquiry_type=None,
	confirmation_status=None,
	campus=None,
	from_date=None,
	to_date=None,
	queue=None,
	query=None,
	limit_start=0,
	limit=80,
):
	_require_school_admin()
	filters = {}
	query = str(query or "").strip()
	status = str(status or "").strip()
	queue = str(queue or "").strip()
	order_queue = queue
	if status:
		if status not in INQUIRY_STATUSES:
			frappe.throw(_("Unsupported inquiry status filter."))
		if queue == "needs_scheduling" and status != "Needs Review":
			return {
				"items": [],
				"total": 0,
				"limit_start": max(cint(limit_start), 0),
				"limit": _limit(limit, default=80, max_value=200),
				"has_more": False,
			}
		filters["status"] = status
	elif queue == "post_visit":
		filters["status"] = ["in", INQUIRY_POST_VISIT_STATUSES]
	elif queue == "upcoming":
		filters["status"] = ["in", INQUIRY_OPEN_STATUSES]
	elif queue == "needs_scheduling":
		filters["status"] = "Needs Review"
	if inquiry_type:
		filters["inquiry_type"] = inquiry_type
	confirmation_status = str(confirmation_status or "").strip()
	if confirmation_status:
		if confirmation_status not in TRIAL_CONFIRMATION_STATUSES:
			frappe.throw(_("Unsupported customer confirmation filter."))
		filters["confirmation_status"] = confirmation_status
	if campus:
		filters["campus"] = campus
	if queue == "upcoming":
		from_date = from_date or nowdate()
		to_date = to_date or add_days(nowdate(), 90)
	elif queue == "post_visit":
		to_date = to_date or nowdate()
	if from_date and to_date:
		filters["current_appointment_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["current_appointment_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["current_appointment_date"] = ["<=", getdate(to_date)]
	search_fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"submitted_student_name",
		],
	)
	or_filters = [["Inquiry", fieldname, "like", f"%{query}%"] for fieldname in search_fields] if query else None

	fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"inquiry_type",
			"status",
			"confirmation_status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"submitted_student_name",
			"preferred_course",
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
			"converted_enrollment",
			"converted_invoice",
			"modified",
		],
	)
	page_limit = _limit(limit, default=80, max_value=200)
	page_start = max(cint(limit_start), 0)
	count_rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		or_filters=or_filters,
		fields=["count(name) as total"],
		limit=1,
	)
	total = cint(count_rows[0].get("total")) if count_rows else 0
	rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by=_inquiry_order_by(order_queue),
		limit_start=page_start,
		limit_page_length=page_limit,
	)
	return {
		"items": [_build_inquiry_list_item(row) for row in rows],
		"total": total,
		"limit_start": page_start,
		"limit": page_limit,
		"has_more": page_start + len(rows) < total,
	}


def get_school_admin_inquiry_data(inquiry=None):
	_require_school_admin()
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	return build_inquiry_detail(inquiry)


def create_school_admin_trial_inquiry_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	family_mode = str(payload.get("family_mode") or "").strip().lower()
	if family_mode not in {"existing", "new"}:
		frappe.throw(_("Family mode must be existing or new."))

	course_session = str(payload.get("course_session") or "").strip()
	if not course_session:
		frappe.throw(_("Course Session is required."))

	if family_mode == "existing":
		family_payload = _manual_trial_existing_family_payload(payload)
	else:
		family_payload = _manual_trial_new_family_payload(payload)

	trial_payload = {
		**family_payload,
		"inquiry_type": "Trial Lesson",
		"course_session": course_session,
		"require_bookable_session": True,
		"prevent_duplicate_student_session": True,
	}
	detail = create_inquiry_core(
		trial_payload,
		source="Manual",
		actor=frappe.session.user,
		commit=False,
	)
	inquiry = (detail.get("inquiry") or {}).get("id")
	note = str(payload.get("note") or "").strip()
	if note:
		detail = add_inquiry_note_core(inquiry, note, actor=frappe.session.user, commit=False)
	frappe.db.commit()
	return detail


def _manual_trial_existing_family_payload(payload):
	parent = str(payload.get("parent") or "").strip()
	student = str(payload.get("student") or "").strip()
	if not parent or not student:
		frappe.throw(_("Parent and Student are required for an existing family."))
	if not frappe.db.exists("Parent", parent):
		frappe.throw(_("Parent was not found."))
	if not frappe.db.exists("Student", student):
		frappe.throw(_("Student was not found."))

	parent_field = _student_parent_field()
	student_parent = frappe.db.get_value("Student", student, parent_field) if parent_field else None
	if student_parent != parent:
		frappe.throw(_("The selected Student does not belong to the selected Parent."))

	parent_doc = frappe.get_doc("Parent", parent)
	linked_user = parent_doc.get("linked_user")
	contact_email = frappe.db.get_value("User", linked_user, "email") if linked_user else None
	contact_phone = parent_doc.get("mobile_number") or parent_doc.get("phone")
	return {
		"parent": parent,
		"student": student,
		"contact_name": parent_doc.get("parent_name") or parent_doc.name,
		"contact_email": contact_email,
		"contact_phone": contact_phone,
	}


def _manual_trial_new_family_payload(payload):
	parent_name = str(payload.get("parent_name") or "").strip()
	contact_email = str(payload.get("contact_email") or payload.get("email") or "").strip().lower()
	student_name = str(payload.get("student_name") or "").strip()
	date_of_birth = payload.get("date_of_birth")
	missing = []
	if not parent_name:
		missing.append(_("Parent name"))
	if not contact_email:
		missing.append(_("Parent email"))
	if not student_name:
		missing.append(_("Student name"))
	if not date_of_birth:
		missing.append(_("Student date of birth"))
	if missing:
		frappe.throw(_("Required fields are missing: {0}.").format(", ".join(missing)))
	validate_email_address(contact_email, throw=True)
	return {
		"parent_name": parent_name,
		"contact_name": parent_name,
		"contact_email": contact_email,
		"contact_phone": str(payload.get("contact_phone") or payload.get("phone") or "").strip(),
		"student_name": student_name,
		"date_of_birth": date_of_birth,
		"student_status": "Inactive",
	}


def add_school_admin_inquiry_note_data(inquiry=None, note=None):
	_require_school_admin()
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)

def send_school_admin_trial_class_reminder_data(inquiry=None):
	_require_school_admin()
	return send_trial_class_reminder_core(inquiry=inquiry)


def update_school_admin_inquiry_confirmation_data(
	inquiry=None,
	confirmation_status=None,
	expected_course_session=None,
):
	_require_school_admin()
	return update_inquiry_confirmation_core(
		inquiry=inquiry,
		confirmation_status=confirmation_status,
		expected_course_session=expected_course_session,
		actor=frappe.session.user,
	)



def update_school_admin_inquiry_status_data(inquiry=None, status=None):
	_require_school_admin()
	status = (status or "").strip()
	if status not in {"Cancelled", "Completed", "No-show", "Follow-up"}:
		frappe.throw(_("Unsupported inquiry status."))
	return mark_inquiry_status_core(inquiry, status, actor=frappe.session.user)


def mark_school_admin_inquiry_completed_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="Completed")


def mark_school_admin_inquiry_no_show_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="No-show")


def mark_school_admin_inquiry_follow_up_data(inquiry=None):
	return update_school_admin_inquiry_status_data(inquiry=inquiry, status="Follow-up")


def mark_school_admin_inquiry_inactive_data(inquiry=None, inactive_reason=None):
	_require_school_admin()
	return mark_inquiry_inactive_core(inquiry, inactive_reason, actor=frappe.session.user)


def reschedule_school_admin_inquiry_data(inquiry=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	inquiry = inquiry or payload.get("inquiry")
	return reschedule_inquiry_core(inquiry, payload, actor=frappe.session.user)


def get_school_admin_conversion_sessions_data(inquiry=None, start_date=None, course=None, campus=None):
	_require_school_admin()
	return get_conversion_session_options(
		inquiry=inquiry,
		start_date=start_date,
		course=course,
		campus=campus,
	)


def convert_school_admin_inquiry_data(inquiry=None, course_session=None):
	_require_school_admin()
	return convert_inquiry_to_full_term_core(inquiry, course_session, actor=frappe.session.user)


def get_school_admin_invoices_data(status=None, customer=None, parent=None, student=None, source=None, limit=80):
	_require_school_admin()
	return {
		"items": _get_invoice_rows(
			status=status,
			customer=customer,
			parent=parent,
			students=[student] if student else None,
			source=source,
			limit=_limit(limit, default=80, max_value=200),
		)
	}


def get_school_admin_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	return _build_invoice_payload(doc)


def create_school_admin_manual_invoice_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	customer = payload.get("customer")
	items = payload.get("items") or []
	if not customer:
		frappe.throw(_("Customer is required."))
	if not items:
		frappe.throw(_("At least one invoice item is required."))

	disable_sales_invoice_auto_notifications()
	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = customer
	apply_default_invoice_dates(invoice)
	if payload.get("due_date"):
		invoice.due_date = payload.get("due_date")
	_set_if_field(invoice, "parent", payload.get("parent"))
	_set_if_field(invoice, "student", payload.get("student"))
	_set_if_field(invoice, "primary_student", payload.get("student"))
	_set_if_field(invoice, "enrollment", payload.get("enrollment"))
	_set_if_field(invoice, "course", payload.get("course"))
	_set_if_field(invoice, "qas_invoice_type", payload.get("qas_invoice_type") or payload.get("invoice_type") or "Other")
	_set_if_field(invoice, "source_doctype", payload.get("source_doctype"))
	_set_if_field(invoice, "source_document", payload.get("source_document"))
	_set_if_field(invoice, "billing_note", payload.get("billing_note"))
	_set_if_field(invoice, "source_type", payload.get("source_type") or "Manual")
	_set_if_field(invoice, "qas_is_manual_invoice", 1)
	_set_if_field(
		invoice,
		"qas_apply_store_credit_on_submit",
		cint(payload.get("apply_store_credit_on_submit", 1)),
	)
	_set_if_field(invoice, "remarks", payload.get("remarks"))
	_apply_invoice_payment_payload(invoice, payload)
	apply_invoice_payment_snapshot(invoice)
	_apply_invoice_items(invoice, items)
	_sync_invoice_student_summary(invoice)
	_run_school_admin_invoice_mutation(lambda: invoice.insert(ignore_permissions=True))
	_add_comment("Sales Invoice", invoice.name, "Manual invoice created by School Admin.")
	frappe.db.commit()
	return _build_invoice_payload(invoice)


def update_school_admin_draft_invoice_data(invoice=None, payload=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be edited."))
	is_manual_invoice = cint(doc.get("qas_is_manual_invoice")) or (doc.get("source_type") or "").strip().lower() == "manual"

	for fieldname in ["customer", "due_date", "remarks"]:
		if fieldname in payload:
			doc.set(fieldname, payload.get(fieldname))
	for fieldname in [
		"parent",
		"student",
		"primary_student",
		"enrollment",
		"course",
		"term",
		"qas_invoice_type",
		"source_doctype",
		"source_document",
		"billing_note",
		"source_inquiry",
		"source_type",
	]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if "apply_store_credit_on_submit" in payload and is_manual_invoice:
		_set_if_field(
			doc,
			"qas_apply_store_credit_on_submit",
			cint(payload.get("apply_store_credit_on_submit")),
		)
	_apply_invoice_payment_payload(doc, payload)
	apply_invoice_payment_snapshot(doc)
	if "items" in payload:
		_apply_invoice_items(doc, payload.get("items") or [])
	_sync_invoice_student_summary(doc)
	_run_school_admin_invoice_mutation(lambda: doc.save(ignore_permissions=True))
	_add_comment("Sales Invoice", doc.name, "Draft invoice updated by School Admin.")
	frappe.db.commit()
	return _build_invoice_payload(doc)


def delete_school_admin_draft_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be deleted. Cancel submitted invoices instead."))
	_detach_invoice_operation_report_links(doc.name)
	_clear_deleted_invoice_enrollment_snapshot(doc)
	deleted = doc.name
	frappe.delete_doc("Sales Invoice", deleted, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": deleted}


def submit_school_admin_invoice_data(invoice=None, enqueue_notification=False, send_notifications=True):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	send_notifications = cint(send_notifications)
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be submitted."))

	def submit_invoice():
		if apply_invoice_payment_snapshot(doc):
			doc.save(ignore_permissions=True)
		doc.flags.ignore_permissions = True
		doc.submit()

	_run_school_admin_invoice_mutation(submit_invoice)
	_add_comment("Sales Invoice", doc.name, "Invoice approved and submitted by School Admin.")
	application = apply_store_credit_to_invoice(doc)
	if flt(application.get("applied")) > 0:
		_add_comment("Sales Invoice", doc.name, _("Store credit applied: {0}.").format(flt(application.get("applied"))))
	doc = frappe.get_doc("Sales Invoice", doc.name)
	sync_invoice_store_credit_snapshot(doc)
	applied_amount = flt(get_invoice_store_credit_applied(doc.name))
	frappe.db.commit()
	doc = frappe.get_doc("Sales Invoice", doc.name)
	receipt_notification = None
	if not send_notifications:
		_add_comment("Sales Invoice", doc.name, "Invoice submitted without parent notifications by School Admin.")
		notification = _skipped_invoice_notification("Parent notifications were skipped for this submission.")
		receipt_notification = _skipped_invoice_notification("Parent notifications were skipped for this submission.", receipt=True)
	elif enqueue_notification:
		notification = _enqueue_invoice_notification(doc, event="approved", store_credit_applied=applied_amount if applied_amount > 0 else None)
	else:
		notification = _send_invoice_notification(doc, event="approved", store_credit_applied=applied_amount if applied_amount > 0 else None)
	if send_notifications:
		receipt_notification = (
			_enqueue_paid_receipt(doc, source="invoice_submit")
			if enqueue_notification
			else _maybe_send_paid_receipt(doc, source="invoice_submit")
		)
	frappe.db.commit()
	payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", doc.name))
	payload["store_credit_application"] = application
	payload["notification"] = notification
	payload["receipt_notification"] = receipt_notification
	return payload


def resend_school_admin_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	if apply_invoice_payment_snapshot(doc):
		_run_school_admin_invoice_mutation(lambda: doc.save(ignore_permissions=True))
	sync_invoice_store_credit_snapshot(doc)
	doc = frappe.get_doc("Sales Invoice", invoice)
	frappe.db.commit()
	notification = _send_invoice_notification(doc, event="resent")
	_add_comment("Sales Invoice", doc.name, "Invoice resent to parent by School Admin.")
	frappe.db.commit()
	payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))
	payload["notification"] = notification
	return payload


def mark_school_admin_invoice_paid_data(invoice=None, payload=None):
	_require_school_admin()
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))
	if not invoice:
		frappe.throw(_("Invoice is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted invoices can be paid."))

	amount = flt(payload.get("amount") or payload.get("paid_amount") or get_invoice_payable_amount(doc) or doc.outstanding_amount)
	if amount <= 0:
		frappe.throw(_("Payment amount is required."))

	payment_entry = _create_payment_entry_for_invoice(
		doc,
		amount=amount,
		mode_of_payment=payload.get("mode_of_payment"),
		reference_no=payload.get("reference_no"),
		notes=payload.get("notes"),
	)
	promotion_bonus = grant_store_credit_bonus_for_payment_entry(payment_entry)
	_add_comment(
		"Sales Invoice",
		doc.name,
		_("Payment recorded by School Admin: {0}.").format(payment_entry.name),
	)
	sync_invoice_store_credit_snapshot(doc.name)
	frappe.db.commit()
	doc = frappe.get_doc("Sales Invoice", invoice)
	receipt_notification = _enqueue_paid_receipt(doc, payment_entry=payment_entry, source="mark_paid")
	frappe.db.commit()
	payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))
	payload["receipt_notification"] = receipt_notification
	payload["payment_entry"] = payment_entry.name
	payload["promotion_bonus"] = promotion_bonus or {
		"entry": get_store_credit_bonus_for_source("Payment Entry", payment_entry.name)
	}
	return payload


def cancel_school_admin_invoice_data(invoice=None, reason=None, allow_empty_reason=False, send_notifications=True):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	reason = (reason or "").strip()
	send_notifications = cint(send_notifications)
	if not reason and not allow_empty_reason:
		frappe.throw(_("Cancellation reason is required."))

	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) == 2:
		_clear_deleted_invoice_enrollment_snapshot(doc, action="cancelled")
		frappe.db.commit()
		payload = _build_invoice_payload(doc)
		payload["cancellation_notification"] = _skipped_invoice_notification("Invoice was already cancelled; no duplicate notification was sent.")
		return payload
	if cint(doc.docstatus) == 0:
		frappe.throw(_("Draft invoices cannot be cancelled. Delete the draft instead."))
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted invoices can be cancelled."))
	if cint(doc.docstatus) == 1:
		if not payment_mutations_enabled():
			frappe.throw(_(payment_block_reason()))
		paid_credit_amount = _invoice_payment_amount(doc.name)
		_cancel_invoice_payment_entries(doc.name)
		cancel_store_credit_journal_entries(doc.name)
		_reverse_invoice_store_credit_application(doc, reason)
		paid_credit = _create_invoice_cancellation_store_credit(doc, paid_credit_amount, reason)
		_cancel_submitted_invoice_as_admin(doc.name)
		_clear_deleted_invoice_enrollment_snapshot(frappe.get_doc("Sales Invoice", doc.name), action="cancelled")
		comment = _("Invoice cancelled by School Admin.")
		if reason:
			comment = _("{0} Reason: {1}").format(comment, reason)
		_add_comment("Sales Invoice", doc.name, comment)
		frappe.db.commit()
		doc = frappe.get_doc("Sales Invoice", invoice)
		if send_notifications:
			try:
				notification = enqueue_parent_invoice_cancellation_notification(doc, reason=reason)
				_add_comment("Sales Invoice", doc.name, "Parent cancellation notification queued by School Admin.")
			except Exception:
				frappe.log_error(frappe.get_traceback(), f"QAS invoice cancellation notification queue failed: {doc.name}")
				_add_comment("Sales Invoice", doc.name, "Parent cancellation notification could not be queued; invoice cancellation remains completed.")
				notification = {"sent": False, "queued": False, "reason": "Cancellation completed, but the parent notification could not be queued."}
		else:
			_add_comment("Sales Invoice", doc.name, "Parent cancellation notification skipped by School Admin.")
			notification = _skipped_invoice_notification("Parent cancellation notification was skipped by School Admin.")
		frappe.db.commit()
		payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))
		payload["cancellation_store_credit_amount"] = paid_credit_amount if paid_credit else 0
		payload["cancellation_store_credit"] = paid_credit.name if paid_credit else None
		payload["cancellation_notification"] = notification
		return payload

def reopen_school_admin_unpaid_invoice_data(invoice=None, reason=None):
	"""Cancel an unpaid invoice and return its amendment as the editable draft."""
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A correction reason is required."))
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))

	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) == 2:
		amendment = _invoice_amendment_for(doc.name)
		if amendment:
			payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", amendment))
			payload["already_reopened"] = True
			payload["original_invoice"] = doc.name
			return payload
		frappe.throw(_("Cancelled invoices cannot be reopened. Create a replacement invoice instead."))
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted invoices can be reopened for correction."))

	_reopen_unpaid_invoice_safety_check(doc)
	savepoint = "school_admin_reopen_invoice"
	frappe.db.savepoint(savepoint)
	try:
		# Check again immediately before the cancellation so a late payment or credit
		# application cannot be silently disconnected from the corrected invoice.
		doc = frappe.get_doc("Sales Invoice", invoice)
		_reopen_unpaid_invoice_safety_check(doc)
		_cancel_submitted_invoice_as_admin(doc.name)
		amendment = frappe.copy_doc(frappe.get_doc("Sales Invoice", doc.name))
		amendment.amended_from = doc.name
		if _has_field("Sales Invoice", "status"):
			amendment.status = "Draft"
		amendment.flags.ignore_permissions = True
		_run_school_admin_invoice_mutation(lambda: amendment.insert(ignore_permissions=True))
		_move_enrollment_invoice_snapshots_to_amendment(doc, amendment, reason)
		_add_comment(
			"Sales Invoice",
			doc.name,
			_("Invoice cancelled for correction. Amendment draft: {0}. Reason: {1}").format(amendment.name, reason),
		)
		_add_comment(
			"Sales Invoice",
			amendment.name,
			_("Amendment draft created from cancelled invoice {0}. Reason: {1}").format(doc.name, reason),
		)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", amendment.name))
	payload["original_invoice"] = doc.name
	payload["reopened"] = True
	return payload


def start_school_admin_bulk_invoice_submit_job_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	if payload.get("all_drafts"):
		invoice_names = _get_all_draft_invoice_names()
	else:
		invoices = payload.get("invoices") or []
		if not isinstance(invoices, list):
			frappe.throw(_("Invoices must be a list."))
		invoice_names = _unique_invoice_names(invoices)
	if not invoice_names:
		frappe.throw(_("At least one draft invoice is required."))

	job_id = frappe.generate_hash(length=16)
	status = _bulk_invoice_submit_initial_status(job_id, invoice_names)
	_set_bulk_invoice_submit_job_status(job_id, status)
	frappe.enqueue(
		"qas_custom.services.school_admin.run_school_admin_bulk_invoice_submit_job",
		queue="long",
		timeout=3600,
		job_name=f"QAS Bulk Invoice Submit {job_id}",
		enqueue_after_commit=True,
		qas_job_id=job_id,
		invoices=invoice_names,
		requested_by=frappe.session.user,
	)
	return status


def _unique_invoice_names(invoices):
	invoice_names = []
	seen = set()
	for invoice in invoices or []:
		invoice_name = (invoice or "").strip()
		if invoice_name and invoice_name not in seen:
			seen.add(invoice_name)
			invoice_names.append(invoice_name)
	return invoice_names


def _draft_invoice_submit_filters():
	filters = {"docstatus": 0}
	if _has_field("Sales Invoice", "status"):
		filters["status"] = ["!=", "Cancelled"]
	return filters


def _get_all_draft_invoice_names():
	return frappe.get_all(
		"Sales Invoice",
		filters=_draft_invoice_submit_filters(),
		pluck="name",
		order_by="creation asc",
		limit_page_length=0,
	)


def get_school_admin_bulk_invoice_submit_job_data(job_id=None):
	_require_school_admin()
	job_id = (job_id or "").strip()
	if not job_id:
		frappe.throw(_("Job ID is required."))
	status = _get_bulk_invoice_submit_job_status(job_id)
	if not status:
		frappe.throw(_("Bulk invoice submit job was not found or has expired."))
	return status


def run_school_admin_bulk_invoice_submit_job(qas_job_id=None, invoices=None, requested_by=None):
	job_id = (qas_job_id or "").strip()
	invoice_names = invoices or []
	if not job_id:
		return

	if requested_by:
		frappe.set_user(requested_by)

	status = _get_bulk_invoice_submit_job_status(job_id) or _bulk_invoice_submit_initial_status(job_id, invoice_names)
	status.update({"status": "running", "started_at": now_datetime().isoformat(), "current_invoice": None})
	_set_bulk_invoice_submit_job_status(job_id, status)

	for invoice_name in invoice_names:
		invoice_name = (invoice_name or "").strip()
		if not invoice_name:
			continue
		status["current_invoice"] = invoice_name
		_set_bulk_invoice_submit_job_status(job_id, status)
		try:
			result_row = _run_one_bulk_invoice_submit(invoice_name)
			status["results"].append(result_row)
			status["processed"] += 1
			if result_row.get("skipped"):
				status["skipped"] += 1
			elif result_row.get("ok"):
				status["succeeded"] += 1
			else:
				status["failed"] += 1
		except Exception as exc:
			frappe.db.rollback()
			status["processed"] += 1
			status["failed"] += 1
			status["results"].append(
				{
					"invoice": invoice_name,
					"ok": False,
					"message": _bulk_action_error_message(exc),
				}
			)
		_set_bulk_invoice_submit_job_status(job_id, status)

	status["current_invoice"] = None
	status["completed_at"] = now_datetime().isoformat()
	status["status"] = "completed_with_errors" if status.get("failed") else "completed"
	_set_bulk_invoice_submit_job_status(job_id, status)
	return status


def _run_one_bulk_invoice_submit(invoice_name):
	if not frappe.db.exists("Sales Invoice", invoice_name):
		return {"invoice": invoice_name, "ok": False, "message": _("Invoice was not found.")}

	docstatus = cint(frappe.db.get_value("Sales Invoice", invoice_name, "docstatus"))
	if docstatus == 1:
		return {"invoice": invoice_name, "ok": True, "skipped": True, "docstatus": 1, "message": _("Already submitted")}
	if docstatus == 2:
		return {"invoice": invoice_name, "ok": False, "docstatus": 2, "message": _("Cancelled invoices cannot be submitted.")}

	result = submit_school_admin_invoice_data(invoice=invoice_name, enqueue_notification=True)
	return {
		"invoice": invoice_name,
		"ok": True,
		"status": result.get("status"),
		"docstatus": result.get("docstatus"),
		"notification": result.get("notification"),
		"receipt_notification": result.get("receipt_notification"),
		"message": _("Done"),
	}


def _bulk_invoice_submit_initial_status(job_id, invoice_names):
	return {
		"job_id": job_id,
		"status": "queued",
		"total": len(invoice_names or []),
		"processed": 0,
		"succeeded": 0,
		"failed": 0,
		"skipped": 0,
		"current_invoice": None,
		"results": [],
		"created_at": now_datetime().isoformat(),
		"started_at": None,
		"completed_at": None,
	}


def _bulk_invoice_submit_job_cache_key(job_id):
	return f"qas:school_admin:bulk_invoice_submit:{job_id}"


def _set_bulk_invoice_submit_job_status(job_id, status):
	frappe.cache().set_value(
		_bulk_invoice_submit_job_cache_key(job_id),
		status,
		expires_in_sec=BULK_INVOICE_SUBMIT_JOB_TTL_SECONDS,
	)


def _get_bulk_invoice_submit_job_status(job_id):
	return frappe.cache().get_value(_bulk_invoice_submit_job_cache_key(job_id))


def bulk_school_admin_invoice_action_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	action = (payload.get("action") or "").strip().lower()
	invoices = payload.get("invoices") or []
	reason = (payload.get("reason") or "").strip()

	if action not in {"submit", "cancel"}:
		frappe.throw(_("Bulk invoice action must be submit or cancel."))
	if not isinstance(invoices, list) or not invoices:
		frappe.throw(_("At least one invoice is required."))
	if len(invoices) > 100:
		frappe.throw(_("Bulk actions are limited to 100 invoices at a time."))
	if action == "cancel" and not reason:
		frappe.throw(_("Cancellation reason is required."))

	invoice_names = []
	seen_invoice_names = set()
	for invoice in invoices:
		invoice_name = (invoice or "").strip()
		if not invoice_name or invoice_name in seen_invoice_names:
			continue
		seen_invoice_names.add(invoice_name)
		invoice_names.append(invoice_name)
	if not invoice_names:
		frappe.throw(_("At least one invoice is required."))
	if action == "cancel":
		ineligible = []
		for invoice_name in invoice_names:
			doc = frappe.get_doc("Sales Invoice", invoice_name)
			if cint(doc.docstatus) != 1:
				ineligible.append(invoice_name)
		if ineligible:
			frappe.throw(
				_("Bulk cancellation accepts submitted invoices only. Remove: {0}.").format(", ".join(ineligible))
			)

	results = []
	for invoice_name in invoice_names:
		try:
			if action == "submit":
				result = submit_school_admin_invoice_data(invoice=invoice_name, enqueue_notification=True)
			else:
				result = cancel_school_admin_invoice_data(invoice=invoice_name, reason=reason)
			results.append(
				{
					"invoice": invoice_name,
					"ok": True,
					"status": result.get("status"),
					"docstatus": result.get("docstatus"),
					"message": _("Done"),
				}
			)
		except Exception as exc:
			frappe.db.rollback()
			results.append(
				{
					"invoice": invoice_name,
					"ok": False,
					"message": _bulk_action_error_message(exc),
				}
			)

	succeeded = len([row for row in results if row.get("ok")])
	failed = len(results) - succeeded
	return {
		"action": action,
		"total": len(results),
		"succeeded": succeeded,
		"failed": failed,
		"results": results,
	}


def _bulk_action_error_message(exc):
	if frappe.message_log:
		message = frappe.message_log.pop()
		frappe.message_log.clear()
		if isinstance(message, dict):
			return message.get("message") or message.get("title") or str(exc)
		return str(message)
	return str(exc)


def get_school_admin_terms_data(status=None, limit=80):
	_require_school_admin()
	if not _doctype_available("Term"):
		return {"items": []}
	filters = {}
	if status:
		filters["status"] = status
	fields = _safe_fields("Term", ["name", "term_name", "start_date", "end_date", "status", "modified"])
	rows = frappe.get_all(
		"Term",
		filters=filters,
		fields=fields,
		order_by="start_date desc, modified desc",
		limit=_limit(limit, default=80, max_value=200),
	)
	return {"items": [_term_row_payload(row) for row in rows]}


def get_school_admin_term_data(term=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	doc = frappe.get_doc("Term", term)
	payload = _document_payload(doc)
	payload["weekly_timeslot_count"] = _count("Weekly Timeslot", {"term": term})
	timeslot_ids = frappe.get_all("Weekly Timeslot", filters={"term": term}, pluck="name", limit_page_length=0)
	payload["course_session_count"] = (
		_count("Course Sessions", {"weekly_timeslot": ["in", timeslot_ids]}) if timeslot_ids else 0
	)
	payload["active_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Active"})
	payload["planned_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Planned"})
	payload["planned_enrollments"] = _get_enrollment_rows(filters={"term": term, "status": "Planned"}, limit=500)
	payload["weekly_timeslots"] = get_school_admin_weekly_timeslots_data(
		term=term,
		include_inactive_terms=1,
		include_inactive_timeslots=1,
		limit=300,
	).get("items", [])
	payload["timeslot_options"] = _get_weekly_timeslot_reference_options()
	return payload


def create_school_admin_term_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	doc = frappe.new_doc("Term")
	for fieldname in ["term_name", "start_date", "end_date", "status", "notes"]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if not doc.get("term_name"):
		frappe.throw(_("Term name is required."))
	if not doc.get("start_date") or not doc.get("end_date"):
		frappe.throw(_("Term start and end dates are required."))
	if getdate(doc.end_date) < getdate(doc.start_date):
		frappe.throw(_("Term end date cannot be before start date."))
	if not doc.get("status"):
		_set_if_field(doc, "status", "Upcoming")
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return get_school_admin_term_data(doc.name)


def update_school_admin_term_data(term=None, payload=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Term", term)
	for fieldname in ["term_name", "start_date", "end_date", "status", "notes"]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if not doc.get("term_name"):
		frappe.throw(_("Term name is required."))
	if not doc.get("start_date") or not doc.get("end_date"):
		frappe.throw(_("Term start and end dates are required."))
	if getdate(doc.end_date) < getdate(doc.start_date):
		frappe.throw(_("Term end date cannot be before start date."))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return get_school_admin_term_data(doc.name)




def delete_school_admin_term_data(term=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	_assert_safe_delete("Term", term)
	frappe.delete_doc("Term", term, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": term}

def copy_school_admin_term_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	source_term = payload.get("source_term")
	target_term = payload.get("target_term")
	if not source_term or not target_term:
		frappe.throw(_("Source term and target term are required."))
	if source_term == target_term:
		frappe.throw(_("Source term and target term must be different."))
	if not frappe.db.exists("Term", source_term) or not frappe.db.exists("Term", target_term):
		frappe.throw(_("Source or target term does not exist."))

	timeslot_map = _copy_term_weekly_timeslots(source_term, target_term)
	planned_count = _copy_term_planned_enrollments(source_term, target_term, timeslot_map)
	frappe.db.commit()
	return {
		"term": get_school_admin_term_data(target_term),
		"summary": {
			"copied_timeslots": len(set(timeslot_map.values())),
			"planned_enrollments": planned_count,
		},
	}


def populate_school_admin_term_data(term=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	return _populate_planned_enrollments_for_term(term)


def populate_school_admin_term_sessions_data(term=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	created_sessions = _generate_sessions_for_term(term)
	frappe.db.commit()
	return {
		"term": get_school_admin_term_data(term),
		"summary": {
			"created_sessions": len(created_sessions),
		},
	}


def create_school_admin_term_attendance_data(term=None, payload=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	payload = _get_payload(payload)
	names = _get_attendance_candidate_enrollment_names(term=term)
	summary = _create_attendance_for_enrollment_names(names, payload=payload)
	frappe.db.commit()
	return {
		"term": get_school_admin_term_data(term),
		"summary": summary,
	}


def _term_row_payload(row):
	payload = _normalize_row_payload("Term", row)
	term = payload.get("name")
	if term:
		payload["weekly_timeslot_count"] = _count("Weekly Timeslot", {"term": term})
		payload["active_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Active"})
		payload["planned_enrollment_count"] = _count("Enrollment", {"term": term, "status": "Planned"})
	return payload


def _term_summary(term):
	if not term or not frappe.db.exists("Term", term):
		return None
	fields = _safe_fields("Term", ["name", "term_name", "start_date", "end_date", "status"])
	rows = frappe.get_all("Term", filters={"name": term}, fields=fields, limit=1)
	return _term_row_payload(rows[0]) if rows else None


def _copy_term_weekly_timeslots(source_term, target_term):
	fields = _safe_fields(
		"Weekly Timeslot",
		[
			"name",
			"term",
			"course",
			"class_language",
			"campus",
			"classroom",
			"teacher",
			"day_of_week",
			"start_time",
			"end_time",
			"status",
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters={"term": source_term},
		fields=fields,
		order_by="course asc, campus asc, day_of_week asc, start_time asc",
		limit_page_length=0,
	)
	timeslot_map = {}
	for row in rows:
		existing = _matching_target_timeslot(row, target_term)
		if existing:
			timeslot_map[row.name] = existing
			continue
		doc = frappe.new_doc("Weekly Timeslot")
		for fieldname in fields:
			if fieldname in {"name", "term"}:
				continue
			_set_if_field(doc, fieldname, row.get(fieldname))
		_set_if_field(doc, "term", target_term)
		doc.insert(ignore_permissions=True)
		timeslot_map[row.name] = doc.name
	return timeslot_map


def _matching_target_timeslot(source_row, target_term):
	filters = {"term": target_term}
	for fieldname in ["course", "campus", "classroom", "teacher", "day_of_week", "start_time"]:
		if _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = source_row.get(fieldname)
	return frappe.db.exists("Weekly Timeslot", filters)


def _source_term_enrollments_for_planning(source_term):
	fields = _safe_fields(
		"Enrollment",
		[
			"name",
			"student",
			"parent",
			"term",
			"course",
			"weekly_timeslot",
			"enrollment_type",
			"status",
		],
	)
	return frappe.get_all(
		"Enrollment",
		filters={"term": source_term, "status": "Active", "enrollment_type": "Full-Term"},
		fields=fields,
		order_by="weekly_timeslot asc, student asc",
		limit_page_length=0,
	)


def _copy_term_planned_enrollments(source_term, target_term, timeslot_map):
	planned_count = 0
	target_term_doc = frappe.get_doc("Term", target_term)
	for enrollment in _source_term_enrollments_for_planning(source_term):
		target_timeslot = timeslot_map.get(enrollment.get("weekly_timeslot"))
		if not target_timeslot:
			continue
		if _existing_target_enrollment(enrollment.get("student"), target_term, target_timeslot, statuses=["Planned", "Active"]):
			continue
		doc = frappe.new_doc("Enrollment")
		_apply_enrollment_payload(
			doc,
			{
				"student": enrollment.get("student"),
				"parent": enrollment.get("parent"),
				"term": target_term,
				"course": enrollment.get("course"),
				"weekly_timeslot": target_timeslot,
				"enrollment_type": enrollment.get("enrollment_type") or "Full-Term",
				"status": "Planned",
				"enrollment_date": target_term_doc.get("start_date") or today(),
			},
		)
		doc.insert(ignore_permissions=True)
		_add_comment("Enrollment", doc.name, _("Planned enrollment copied from {0}.").format(enrollment.get("name")))
		planned_count += 1
	return planned_count


def _existing_target_enrollment(student, term, weekly_timeslot, statuses=None):
	if not student or not term or not weekly_timeslot:
		return None
	filters = {
		"student": student,
		"term": term,
		"weekly_timeslot": weekly_timeslot,
	}
	if statuses:
		filters["status"] = ["in", statuses]
	return frappe.db.exists("Enrollment", filters)


def _generate_sessions_for_term(term):
	term_doc = frappe.get_doc("Term", term)
	if not term_doc.get("start_date") or not term_doc.get("end_date"):
		frappe.throw(_("Target term dates are required before generating sessions."))
	filters = {"term": term}
	if _has_field("Weekly Timeslot", "status"):
		filters["status"] = "Active"
	timeslots = frappe.get_all(
		"Weekly Timeslot",
		filters=filters,
		fields=_safe_fields("Weekly Timeslot", ["name", "day_of_week"]),
		limit_page_length=0,
	)
	created = []
	for timeslot in timeslots:
		current = getdate(term_doc.start_date)
		end_date = getdate(term_doc.end_date)
		target_weekday = _weekday_number(timeslot.day_of_week)
		while current <= end_date:
			if current.weekday() == target_weekday:
				session = _ensure_course_session(timeslot.name, current)
				if session.get("created"):
					created.append(session.get("name"))
			current = current + timedelta(days=1)
	return created


def _populate_planned_enrollments_for_term(term):
	term_doc = frappe.get_doc("Term", term)
	created_sessions = _generate_sessions_for_term(term)
	activated_enrollments = 0
	created_attendance_entries = 0
	skipped = 0
	errors = 0
	error_rows = []

	planned_rows = frappe.get_all(
		"Enrollment",
		filters={"term": term, "status": "Planned", "enrollment_type": "Full-Term"},
		fields=_safe_fields("Enrollment", ["name", "student", "weekly_timeslot", "course"]),
		order_by="weekly_timeslot asc, student asc",
		limit_page_length=0,
	)
	for row in planned_rows:
		savepoint = f"planned_enrollment_{row.name}".replace("-", "_")
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.get_doc("Enrollment", row.name)
			result = _activate_planned_enrollment(doc, term_doc)
			activated_enrollments += 1
			created_attendance_entries += cint(result.get("attendance_entries") or 0)
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			errors += 1
			error_rows.append({"enrollment": row.name, "error": _bulk_action_error_message(exc)})

	frappe.db.commit()
	return {
		"term": get_school_admin_term_data(term),
		"summary": {
			"created_sessions": len(created_sessions),
			"created_enrollments": activated_enrollments,
			"created_attendance_entries": created_attendance_entries,
			"skipped": skipped,
			"errors": errors,
			"error_rows": error_rows,
		},
	}


def _activate_planned_enrollment(enrollment, term_doc, start_session=None):
	if enrollment.get("status") != "Planned":
		return {"enrollment": enrollment.name, "invoice": enrollment.get("invoice")}
	if not enrollment.get("weekly_timeslot"):
		frappe.throw(_("Weekly timeslot is required before activating enrollment."))
	timeslot = frappe.db.get_value(
		"Weekly Timeslot",
		enrollment.weekly_timeslot,
		_safe_fields("Weekly Timeslot", ["name", "term", "course"]),
		as_dict=True,
	)
	if not timeslot:
		frappe.throw(_("Weekly timeslot does not exist."))
	if timeslot.get("term") != term_doc.name:
		frappe.throw(_("Weekly timeslot does not belong to this term."))
	duplicate = _duplicate_active_enrollment(
		enrollment.student,
		term_doc.name,
		enrollment.weekly_timeslot,
		exclude=enrollment.name,
	)
	if duplicate:
		frappe.throw(_("An active enrollment already exists for this student and weekly timeslot."))
	start_session = _validate_enrollment_start_session(
		start_session or _first_course_session_for_timeslot(enrollment.weekly_timeslot, term_doc),
		enrollment.weekly_timeslot,
		term_doc,
	)
	_set_if_field(enrollment, "start_course_session", start_session)
	_set_if_field(enrollment, "course", enrollment.get("course") or timeslot.get("course"))
	_set_if_field(enrollment, "status", "Active")
	start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
	_set_if_field(enrollment, "enrollment_date", start_date or term_doc.get("start_date") or enrollment.get("enrollment_date") or today())
	enrollment.save(ignore_permissions=True)
	attendance_entries = _create_enrollment_attendance_entries(enrollment)
	_add_comment("Enrollment", enrollment.name, _("Planned enrollment activated for term {0}.").format(term_doc.name))
	return {
		"enrollment": enrollment.name,
		"attendance_entries": len(attendance_entries),
	}


def _validate_enrollment_start_session(start_session, weekly_timeslot, term_doc):
	if not start_session:
		frappe.throw(_("Start session is required before activating enrollment."))
	session = frappe.db.get_value(
		"Course Sessions",
		start_session,
		["name", "weekly_timeslot", "session_date", "status"],
		as_dict=True,
	)
	if not session:
		frappe.throw(_("Start session was not found."))
	if session.get("weekly_timeslot") != weekly_timeslot:
		frappe.throw(_("Start session does not belong to the selected weekly timeslot."))
	if session.get("status") == "Cancelled":
		frappe.throw(_("Start session is cancelled."))
	session_date = getdate(session.get("session_date"))
	if session_date < getdate(term_doc.start_date) or session_date > getdate(term_doc.end_date):
		frappe.throw(_("Start session is outside the selected term."))
	return session.name


def _duplicate_active_enrollment(student, term, weekly_timeslot, exclude=None):
	filters = {
		"student": student,
		"term": term,
		"weekly_timeslot": weekly_timeslot,
		"status": "Active",
	}
	if exclude:
		filters["name"] = ["!=", exclude]
	return frappe.db.exists("Enrollment", filters)


def _validate_unique_open_enrollment(enrollment):
	if enrollment.get("enrollment_type") != "Full-Term":
		return
	if enrollment.get("status") not in ("Planned", "Active"):
		return
	if not enrollment.get("student") or not enrollment.get("term") or not enrollment.get("weekly_timeslot"):
		return
	filters = {
		"student": enrollment.student,
		"term": enrollment.term,
		"weekly_timeslot": enrollment.weekly_timeslot,
		"enrollment_type": "Full-Term",
		"status": ["in", ["Planned", "Active"]],
	}
	if enrollment.get("name"):
		filters["name"] = ["!=", enrollment.name]
	duplicate = frappe.db.exists("Enrollment", filters)
	if duplicate:
		frappe.throw(_("This student already has an open enrollment for this term and weekly timeslot: {0}.").format(duplicate))


def _first_course_session_for_timeslot(weekly_timeslot, term_doc):
	rows = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": weekly_timeslot,
			"session_date": ["between", [getdate(term_doc.start_date), getdate(term_doc.end_date)]],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "session_date"],
		order_by="session_date asc",
		limit=1,
	)
	if not rows:
		frappe.throw(_("No course sessions were generated for the target weekly timeslot."))
	return rows[0].name


def _create_term_enrollment_invoice(enrollment, start_session):
	parent = enrollment.get("parent")
	course = enrollment.get("course")
	if not parent or not course:
		frappe.throw(_("Parent and course are required before generating an invoice."))
	customer = get_invoice_customer(parent)
	item_code = get_invoice_item(course)
	session_count = _course_session_count_for_enrollment(enrollment, start_session)
	if session_count <= 0:
		frappe.throw(_("No billable sessions found for enrollment."))
	full_term_fee = get_course_money(course, ("full_term_fee", "full_term_price", "term_fee"))
	if full_term_fee <= 0:
		frappe.throw(_("Course full term fee is required before generating an invoice."))
	total_sessions = get_course_number(course, ("total_session_per_term", "total_sessions_per_term", "sessions_per_term")) or session_count
	invoice_amount = _enrollment_invoice_amount(full_term_fee, total_sessions, session_count)

	disable_sales_invoice_auto_notifications()
	invoice_name = _find_draft_family_invoice(parent=parent, customer=customer, term=enrollment.term)
	created = not bool(invoice_name)
	invoice = frappe.get_doc("Sales Invoice", invoice_name) if invoice_name else frappe.new_doc("Sales Invoice")

	if created:
		invoice.customer = customer
		apply_default_invoice_dates(invoice)
		_set_if_field(invoice, "parent", parent)
		_set_if_field(invoice, "qas_invoice_type", "Course")
		_set_if_field(invoice, "source_doctype", "Enrollment")
		_set_if_field(invoice, "source_document", enrollment.name)
		_set_if_field(invoice, "enrollment", enrollment.name)
		_set_if_field(invoice, "term", enrollment.term)
		_set_if_field(invoice, "billing_note", _("Draft course invoice generated from term enrollments."))
	else:
		_set_if_field(invoice, "parent", invoice.get("parent") or parent)
		_set_if_field(invoice, "qas_invoice_type", invoice.get("qas_invoice_type") or "Course")
		_set_if_field(invoice, "source_doctype", invoice.get("source_doctype") or "Enrollment")
		_set_if_field(invoice, "source_document", invoice.get("source_document") or enrollment.name)
		_set_if_field(invoice, "enrollment", invoice.get("enrollment") or enrollment.name)
		_set_if_field(invoice, "term", invoice.get("term") or enrollment.term)

	if _invoice_has_enrollment_item(invoice, enrollment.name):
		invoice.flags.qas_was_created = False
		return invoice

	_append_enrollment_invoice_item(
		invoice,
		enrollment=enrollment,
		start_session=start_session,
		item_code=item_code,
		course=course,
		session_count=session_count,
		amount=invoice_amount,
	)
	_sync_invoice_student_summary(invoice)
	apply_invoice_payment_snapshot(invoice)
	if created:
		_run_school_admin_invoice_mutation(lambda: invoice.insert(ignore_permissions=True))
	else:
		_run_school_admin_invoice_mutation(lambda: invoice.save(ignore_permissions=True))
	invoice.flags.qas_was_created = created
	return invoice


def _find_draft_family_invoice(parent, customer, term):
	if not customer or not term or not _doctype_available("Sales Invoice"):
		return None

	filters = {"customer": customer, "docstatus": 0}
	if _has_field("Sales Invoice", "status"):
		filters["status"] = ["!=", "Cancelled"]
	if parent and _has_field("Sales Invoice", "parent"):
		filters["parent"] = parent
	if _has_field("Sales Invoice", "qas_invoice_type"):
		filters["qas_invoice_type"] = "Course"

	if _has_field("Sales Invoice", "term"):
		header_filters = dict(filters)
		header_filters["term"] = term
		rows = frappe.get_all(
			"Sales Invoice",
			filters=header_filters,
			pluck="name",
			order_by="creation asc",
			limit=1,
		)
		if rows:
			return rows[0]

	invoice_names = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		pluck="name",
		order_by="creation asc",
		limit_page_length=0,
	)
	if not invoice_names or not _doctype_available("Sales Invoice Item") or not _has_field("Sales Invoice Item", "term"):
		return None

	item_filters = {"parent": ["in", invoice_names], "term": term}
	if frappe.db.has_column("Sales Invoice Item", "parenttype"):
		item_filters["parenttype"] = "Sales Invoice"
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters=item_filters,
		fields=["parent"],
		order_by="creation asc",
		limit=1,
	)
	return rows[0].parent if rows else None


def _invoice_has_enrollment_item(invoice, enrollment_name):
	if not enrollment_name:
		return False
	return any(item.get("enrollment") == enrollment_name for item in invoice.get("items", []))


def _enrollment_invoice_amount(full_term_fee, total_sessions, session_count):
	full_term_fee = flt(full_term_fee)
	total_sessions = flt(total_sessions)
	session_count = flt(session_count)
	if total_sessions <= 0 or session_count <= 0:
		return 0
	if session_count >= total_sessions:
		return flt(full_term_fee, 2)
	return flt(full_term_fee * session_count / total_sessions, 2)


def _append_enrollment_invoice_item(invoice, *, enrollment, start_session, item_code, course, session_count, amount):
	student_name = get_student_parent_name(enrollment.student) or enrollment.student
	student_code = get_student_display_code(enrollment.student) or enrollment.student
	schedule = invoice_item_schedule({"weekly_timeslot": enrollment.weekly_timeslot})
	description = build_course_invoice_description(student_name, course, enrollment.term, session_count, schedule=schedule)
	item = invoice.append(
		"items",
		{
			"item_code": item_code,
			"item_name": course,
			"description": description,
			"qty": 1,
			"rate": amount,
		},
	)
	_set_if_field(item, "qas_line_type", "Course Fee")
	_set_if_field(item, "student", enrollment.student)
	_set_if_field(item, "student_display_name", student_name)
	_set_if_field(item, "student_code", student_code)
	_set_if_field(item, "enrollment", enrollment.name)
	_set_if_field(item, "course", course)
	_set_if_field(item, "term", enrollment.term)
	_set_if_field(item, "course_session", get_course_session_snapshot_label(start_session))
	_set_if_field(item, "session_count", session_count)
	return item


def _course_session_count_for_enrollment(enrollment, start_session):
	start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
	if not start_date:
		return 0
	return frappe.db.count(
		"Course Sessions",
		{
			"weekly_timeslot": enrollment.weekly_timeslot,
			"session_date": [">=", getdate(start_date)],
			"status": ["!=", "Cancelled"],
		},
	)


def get_school_admin_enrollments_data(
	student=None,
	parent=None,
	course=None,
	term=None,
	enrollment_type=None,
	status=None,
	statuses=None,
	include_inactive_terms=0,
	limit=80,
):
	_require_school_admin()
	filters = {}
	for fieldname, value in {
		"student": student,
		"parent": parent,
		"course": course,
		"term": term,
		"enrollment_type": enrollment_type,
	}.items():
		if value:
			filters[fieldname] = value
	status_values = _parse_status_list(statuses)
	if status:
		filters["status"] = status
	elif status_values:
		filters["status"] = ["in", status_values]
	else:
		filters["status"] = ["in", ["Planned", "Active"]]
	_apply_active_term_filter(filters, term=term, include_inactive_terms=include_inactive_terms)
	return {"items": _get_enrollment_rows(filters=filters, limit=_limit(limit, default=80, max_value=200))}


def get_school_admin_enrollment_data(enrollment=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	doc = frappe.get_doc("Enrollment", enrollment)
	return _build_enrollment_payload(doc)


def create_school_admin_enrollment_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	doc = frappe.new_doc("Enrollment")
	_apply_enrollment_payload(doc, payload)
	_validate_unique_open_enrollment(doc)
	doc.insert(ignore_permissions=True)
	_add_comment("Enrollment", doc.name, "Enrollment created by School Admin.")
	frappe.db.commit()
	return _build_enrollment_payload(doc)


def update_school_admin_enrollment_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Enrollment", enrollment)
	previous_timeslot = doc.get("weekly_timeslot")
	previous_status = doc.get("status")
	if previous_status == "Planned" and payload.get("status") == "Active":
		frappe.throw(_("Use Create Attendance to activate planned enrollments."))
	_apply_enrollment_payload(doc, payload)
	_validate_unique_open_enrollment(doc)
	doc.save(ignore_permissions=True)
	if previous_status == "Active" and payload.get("weekly_timeslot") and payload.get("weekly_timeslot") != previous_timeslot:
		_cancel_future_enrollment_attendance(doc.name, effective_date=payload.get("effective_date") or today())
	_add_comment("Enrollment", doc.name, "Enrollment updated by School Admin.")
	frappe.db.commit()
	return _build_enrollment_payload(doc)


def activate_school_admin_enrollment_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Enrollment", enrollment)
	if doc.get("status") != "Planned":
		frappe.throw(_("Only planned enrollments can be activated."))
	payload.pop("status", None)
	_apply_enrollment_payload(doc, payload)
	_validate_unique_open_enrollment(doc)
	term_doc = frappe.get_doc("Term", doc.term)
	result = _activate_planned_enrollment(
		doc,
		term_doc,
		start_session=payload.get("start_course_session") or doc.get("start_course_session"),
	)
	frappe.db.commit()
	activated = frappe.get_doc("Enrollment", result["enrollment"])
	return {
		"enrollment": _build_enrollment_payload(activated),
		"attendance_entries": result.get("attendance_entries") or 0,
	}


def create_school_admin_enrollment_attendance_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Enrollment", enrollment)
	if doc.get("status") == "Planned":
		payload.pop("status", None)
		_apply_enrollment_payload(doc, payload)
		_validate_unique_open_enrollment(doc)
		term_doc = frappe.get_doc("Term", doc.term)
		result = _activate_planned_enrollment(
			doc,
			term_doc,
			start_session=payload.get("start_course_session") or doc.get("start_course_session"),
		)
		frappe.db.commit()
		activated = frappe.get_doc("Enrollment", result["enrollment"])
		return {
			"enrollment": _build_enrollment_payload(activated),
			"attendance_entries": result.get("attendance_entries") or 0,
		}
	if doc.get("status") != "Active":
		frappe.throw(_("Set the enrollment to Planned or Active before creating attendance."))
	if payload:
		_apply_enrollment_payload(doc, payload)
		_validate_unique_open_enrollment(doc)
	term_doc = frappe.get_doc("Term", doc.term)
	start_session = _validate_enrollment_start_session(
		payload.get("start_course_session") or doc.get("start_course_session") or _first_course_session_for_timeslot(doc.weekly_timeslot, term_doc),
		doc.weekly_timeslot,
		term_doc,
	)
	_set_if_field(doc, "start_course_session", start_session)
	if not doc.get("enrollment_date"):
		start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
		_set_if_field(doc, "enrollment_date", start_date or term_doc.get("start_date") or today())
	doc.save(ignore_permissions=True)
	attendance_entries = _create_enrollment_attendance_entries(doc)
	_add_comment("Enrollment", doc.name, _("Attendance prepared by School Admin."))
	frappe.db.commit()
	return {
		"enrollment": _build_enrollment_payload(doc),
		"attendance_entries": len(attendance_entries),
	}


def create_school_admin_enrollment_invoice_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Enrollment", enrollment)
	if doc.get("status") != "Active":
		frappe.throw(_("Set the enrollment to Active before creating a draft invoice."))
	existing_invoice = _existing_invoice_for_enrollment(doc)
	if existing_invoice:
		frappe.throw(_("This enrollment already has an invoice: {0}.").format(existing_invoice))
	if not doc.get("weekly_timeslot"):
		frappe.throw(_("Weekly timeslot is required before creating an invoice."))
	term_doc = frappe.get_doc("Term", doc.term)
	persisted_start_session = doc.get("start_course_session")
	requested_start_session = payload.get("start_course_session")
	if requested_start_session and requested_start_session != persisted_start_session:
		frappe.throw(_("Start session has changed. Save the enrollment or create attendance before creating an invoice."))
	if not persisted_start_session:
		frappe.throw(_("Start session is required. Save the enrollment or create attendance before creating an invoice."))
	start_session = _validate_enrollment_start_session(
		persisted_start_session,
		doc.weekly_timeslot,
		term_doc,
	)
	timeslot_course = frappe.db.get_value("Weekly Timeslot", doc.weekly_timeslot, "course")
	_set_if_field(doc, "course", doc.get("course") or timeslot_course)
	if not doc.get("enrollment_date"):
		start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
		_set_if_field(doc, "enrollment_date", start_date or term_doc.get("start_date") or today())

	invoice = _create_term_enrollment_invoice(doc, start_session)
	_set_if_field(doc, "start_course_session", start_session)
	_set_if_field(doc, "invoice", invoice.name)
	_set_if_field(doc, "invoice_status", "Draft")
	_set_if_field(doc, "invoice_amount", invoice.get("grand_total"))
	doc.save(ignore_permissions=True)
	_add_comment("Enrollment", doc.name, _("Draft invoice {0} created by School Admin.").format(invoice.name))
	frappe.db.commit()
	return {"enrollment": _build_enrollment_payload(doc), "invoice": invoice.name}


def create_school_admin_family_attendance_data(parent=None, customer=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	context = _resolve_family_context(parent=parent, customer=customer)
	parent_id = context.get("parent")
	customer_id = context.get("customer")
	students = _get_family_students(parent_id)
	student_ids = [row.get("name") for row in students if row.get("name")]
	if not parent_id and not student_ids:
		frappe.throw(_("Family was not found."))
	names = _get_attendance_candidate_enrollment_names(parent=parent_id, students=student_ids)
	summary = _create_attendance_for_enrollment_names(names, payload=payload)
	frappe.db.commit()
	return {
		"family": get_school_admin_family_data(parent=parent_id, customer=customer_id),
		"summary": summary,
	}


def create_school_admin_family_invoice_data(parent=None, customer=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	context = _resolve_family_context(parent=parent, customer=customer)
	parent_id = context.get("parent")
	customer_id = context.get("customer")
	students = _get_family_students(parent_id)
	student_ids = [row.get("name") for row in students if row.get("name")]
	if not parent_id and not student_ids:
		frappe.throw(_("Family was not found."))
	names = _get_invoice_candidate_enrollment_names(parent=parent_id, students=student_ids)
	summary = _create_invoices_for_enrollment_names(names, payload=payload)
	frappe.db.commit()
	return {
		"family": get_school_admin_family_data(parent=parent_id, customer=customer_id),
		"summary": summary,
	}


def create_school_admin_term_invoices_data(term=None, payload=None):
	_require_school_admin()
	if not term:
		frappe.throw(_("Term is required."))
	if not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	payload = _get_payload(payload)
	names = _get_invoice_candidate_enrollment_names(term=term)
	batch_size = cint(payload.get("batch_size") or 0)
	if batch_size > 0:
		names, batch_meta = _next_invoice_candidate_batch(names, batch_size)
	else:
		batch_meta = {"batch_size": 0, "remaining_before": len(names), "remaining_after": 0, "has_more": False}
	summary = _create_invoices_for_enrollment_names(names, payload=payload)
	summary.update(batch_meta)
	frappe.db.commit()
	return {
		"term": get_school_admin_term_data(term),
		"summary": summary,
	}


def transfer_school_admin_enrollment_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	target_timeslot = payload.get("weekly_timeslot")
	if not target_timeslot:
		frappe.throw(_("Target weekly timeslot is required."))
	doc = frappe.get_doc("Enrollment", enrollment)
	effective_date = getdate(payload.get("effective_date") or today())
	preview = _build_enrollment_transfer_preview(doc, target_timeslot, effective_date)
	if cint(payload.get("preview_only")):
		return {"enrollment": _build_enrollment_payload(doc), "transfer": preview}
	if payload.get("preview_fingerprint") != preview.get("preview_fingerprint"):
		frappe.throw(_("Attendance or class sessions changed after the preview. Preview the transfer again before continuing."))
	if preview.get("retained_marked_count") and not cint(payload.get("confirm_retained_marked")):
		frappe.throw(
			_("Marked attendance exists on or after the effective date. Preview the transfer and confirm that these records will remain in the original class."),
		)

	source_timeslot = doc.get("weekly_timeslot")
	cancelled_count = _cancel_enrollment_attendance_for_sessions(
		doc.name,
		preview.get("source_session_ids") or [],
		statuses=TRANSFER_CANCELLABLE_ATTENDANCE_STATUSES,
	)
	doc.weekly_timeslot = target_timeslot
	target_course = preview.get("target_course")
	target_term = preview.get("target_term")
	_set_if_field(doc, "course", target_course)
	_set_if_field(doc, "term", target_term)
	_set_if_field(doc, "start_course_session", preview.get("target_start_course_session"))
	if _has_field("Enrollment", "status"):
		doc.status = "Active"
	_validate_unique_open_enrollment(doc)
	doc.save(ignore_permissions=True)
	destination = _ensure_transfer_destination_attendance(doc, preview.get("target_session_ids") or [])
	result = {
		**preview,
		"cancelled_count": cancelled_count,
		"destination_created_count": destination.get("created") or 0,
		"destination_reactivated_count": destination.get("reactivated") or 0,
		"destination_retained_count": destination.get("retained") or 0,
	}
	_add_comment(
		"Enrollment",
		doc.name,
		_(
			"Enrollment transferred from {0} to {1} from {2} by {3}. Cancelled {4} unmarked attendance row(s), prepared {5} destination row(s), and retained {6} marked row(s) in the original class."
		).format(
			source_timeslot,
			target_timeslot,
			effective_date,
			frappe.session.user,
			cancelled_count,
			destination.get("total") or 0,
			preview.get("retained_marked_count") or 0,
		),
	)
	frappe.db.commit()
	return {"enrollment": _build_enrollment_payload(doc), "transfer": result}


def end_school_admin_enrollment_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Enrollment", enrollment)
	end_date = payload.get("end_date") or today()
	target_status = payload.get("status")
	if target_status not in {"Inactive", "Completed", "Cancelled"}:
		target_status = "Cancelled" if doc.get("status") == "Planned" else "Inactive"
	doc.status = target_status
	doc.save(ignore_permissions=True)
	_cancel_future_enrollment_attendance(doc.name, effective_date=end_date)
	action = _("cancelled") if target_status == "Cancelled" else _("ended")
	_add_comment("Enrollment", doc.name, _("Enrollment {0} by School Admin from {1}.").format(action, end_date))
	frappe.db.commit()
	return _build_enrollment_payload(doc)


def delete_school_admin_enrollment_data(enrollment=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	doc = frappe.get_doc("Enrollment", enrollment)
	_assert_safe_delete_enrollment(doc)
	deleted = doc.name
	frappe.delete_doc("Enrollment", deleted, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": deleted}


def get_school_admin_weekly_timeslots_data(
	term=None,
	course=None,
	campus=None,
	teacher=None,
	status=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=120,
):
	_require_school_admin()
	if not _doctype_available("Weekly Timeslot"):
		return {"items": []}
	filters = {}
	for fieldname, value in {
		"term": term,
		"course": course,
		"campus": campus,
		"teacher": teacher,
		"status": status,
	}.items():
		if value and _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = value
	_apply_active_term_filter(filters, term=term, include_inactive_terms=include_inactive_terms)
	if not status:
		_apply_active_timeslot_filter(filters, include_inactive_timeslots=include_inactive_timeslots)
	fields = _safe_fields(
		"Weekly Timeslot",
		[
			"name",
			"term",
			"course",
			"class_language",
			"campus",
			"classroom",
			"teacher",
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
			"day_of_week",
			"start_time",
			"end_time",
			"status",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Weekly Timeslot",
		filters=filters,
		fields=fields,
		order_by="term desc, course asc, campus asc, day_of_week asc, start_time asc",
		limit=_limit(limit, default=120, max_value=300),
	)
	items = [_docdict(row) for row in rows]
	_attach_course_labels(items)
	enrollment_counts = _get_active_enrollment_counts_for_timeslots([row.get("name") for row in items])
	for item in items:
		item["active_enrollment_count"] = enrollment_counts.get(item.get("name"), 0)
	return {"items": items}


def get_school_admin_weekly_timeslot_data(weekly_timeslot=None):
	_require_school_admin()
	if not _doctype_available("Weekly Timeslot"):
		frappe.throw(_("Weekly Timeslot is not installed on this site."))
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	payload = _document_payload(doc)
	payload["enrollments"] = _get_enrollment_rows(filters={"weekly_timeslot": weekly_timeslot, "status": "Active"}, limit=200)
	payload["sessions"] = _get_course_session_rows(weekly_timeslot=weekly_timeslot, limit=80)
	return payload


def create_school_admin_weekly_timeslot_data(payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	doc = frappe.new_doc("Weekly Timeslot")
	_apply_weekly_timeslot_payload(doc, payload)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return get_school_admin_weekly_timeslot_data(doc.name)


def update_school_admin_weekly_timeslot_data(weekly_timeslot=None, payload=None):
	_require_school_admin()
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	if "teacher" in payload and payload.get("teacher") != doc.get("teacher") and _weekly_timeslot_has_course_sessions(doc.name):
		frappe.throw(_("Use Change weekly teacher from the Classes workspace after sessions have been created."))
	previous_day_of_week = doc.get("day_of_week")
	_apply_weekly_timeslot_payload(doc, payload)
	doc.save(ignore_permissions=True)
	session_sync = None
	if cint(payload.get("apply_future_sessions")):
		session_sync = _sync_future_course_sessions_for_timeslot(
			doc,
			effective_date=payload.get("effective_date"),
			previous_day_of_week=previous_day_of_week,
		)
	frappe.db.commit()
	result = get_school_admin_weekly_timeslot_data(doc.name)
	if session_sync:
		result["session_sync"] = session_sync
	return result


def generate_school_admin_course_sessions_data(weekly_timeslot=None, from_date=None, to_date=None):
	_require_school_admin()
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	from_dt = getdate(from_date or today())
	to_dt = getdate(to_date or add_days(from_dt, 90))
	if to_dt < from_dt:
		frappe.throw(_("To date cannot be before from date."))
	created = []
	current = from_dt
	target_weekday = _weekday_number(doc.day_of_week)
	while current <= to_dt:
		if current.weekday() == target_weekday:
			session = _ensure_course_session(doc.name, current)
			if session.get("created"):
				created.append(session.get("name"))
		current = current + timedelta(days=1)
	frappe.db.commit()
	return {"weekly_timeslot": weekly_timeslot, "created": created, "created_count": len(created)}


def get_school_admin_course_sessions_data(
	weekly_timeslot=None,
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	status=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=160,
):
	_require_school_admin()
	if not _doctype_available("Course Sessions"):
		return {"items": []}
	return {
		"items": _get_course_session_rows(
			weekly_timeslot=weekly_timeslot,
			term=term,
			course=course,
			campus=campus,
			status=status,
			from_date=from_date,
			to_date=to_date,
			include_inactive_terms=include_inactive_terms,
			include_inactive_timeslots=include_inactive_timeslots,
			limit=_limit(limit, default=160, max_value=3000),
		)
	}


def get_school_admin_course_session_data(course_session=None):
	_require_school_admin()
	if not _doctype_available("Course Sessions"):
		frappe.throw(_("Course Sessions is not installed on this site."))
	if not course_session:
		frappe.throw(_("Course session is required."))
	doc = frappe.get_doc("Course Sessions", course_session)
	payload = _document_payload(doc)
	if payload.get("weekly_timeslot"):
		payload["weekly_timeslot_detail"] = _get_timeslot_summary(payload.get("weekly_timeslot"))
	attendance_rows = _get_school_admin_attendance_rows(
		course_session,
		term=(payload.get("weekly_timeslot_detail") or {}).get("term"),
	)
	attending_attendance_rows = _visible_course_session_attendance_rows(attendance_rows)
	roster_attendance_rows = _roster_course_session_attendance_rows(attendance_rows)
	payload["attendance"] = roster_attendance_rows
	payload["student_count"] = len(attending_attendance_rows)
	payload["trial_count"] = sum(1 for row in attending_attendance_rows if row.get("source_doctype") == "Inquiry")
	payload["leave_count"] = _count_leave_attendance_rows(attendance_rows)
	if payload.get("weekly_timeslot"):
		_timeslot_teacher = (payload.get("weekly_timeslot_detail") or {}).get("teacher")
	payload["teacher"] = payload.get("teacher_override") or _timeslot_teacher
	payload["teacher_assignment_source"] = "Session override" if payload.get("teacher_override") else "Weekly timeslot"
	payload["class_content"] = _get_school_admin_session_content_rows(course_session)
	return payload


def get_school_admin_session_photo_content_data(course_session=None, photo_post=None, photo_idx=None):
	_require_school_admin()
	if not course_session or not photo_post:
		frappe.throw(_("Course session and photo post are required."))

	photo_post_doc = frappe.get_doc("Session Photo Post", photo_post)
	if photo_post_doc.get("course_session") != course_session or photo_post_doc.get("status") != "Published":
		raise frappe.PermissionError

	target_idx = cint(photo_idx)
	if target_idx <= 0:
		raise frappe.PermissionError

	photo_row = next((row for row in photo_post_doc.photos or [] if cint(row.idx) == target_idx), None)
	if not photo_row or not getattr(photo_row, "image", None):
		raise frappe.DoesNotExistError

	return _get_school_admin_file_content(photo_row.image)


def get_school_admin_session_video_content_data(course_session=None, video_post=None):
	_require_school_admin()
	if not course_session or not video_post:
		frappe.throw(_("Course session and video post are required."))

	video_post_doc = frappe.get_doc("Session Video Post", video_post)
	if video_post_doc.get("course_session") != course_session or video_post_doc.get("status") != "Published":
		raise frappe.PermissionError
	if not video_post_doc.get("video"):
		raise frappe.DoesNotExistError

	payload = _get_school_admin_file_content(
		video_post_doc.get("video"),
		fallback_filename=video_post_doc.get("file_name"),
		fallback_content_type=video_post_doc.get("mime_type"),
	)
	payload["display_content_as"] = "inline"
	return payload


def _get_school_admin_session_content_rows(
	course_session,
	*,
	photo_method="qas_custom.api.school_admin.school_admin_get_course_session_photo",
	video_method="qas_custom.api.school_admin.school_admin_get_course_session_video",
):
	items = []
	teacher_ids = set()

	if _doctype_available("Session Homework"):
		for row in frappe.get_all(
			"Session Homework",
			filters={"course_session": course_session, "status": "Published"},
			fields=["name", "title", "description", "published_at", "teacher"],
			order_by="published_at desc, creation desc",
		):
			teacher_ids.add(row.get("teacher"))
			items.append({
				"type": "class_update",
				"id": row.get("name"),
				"title": row.get("title") or _("Class Update"),
				"summary": row.get("description") or "",
				"published_at": _school_admin_content_datetime(row.get("published_at")),
				"teacher": row.get("teacher") or "",
			})

	photo_rows = []
	if _doctype_available("Session Photo Post"):
		photo_rows = frappe.get_all(
			"Session Photo Post",
			filters={"course_session": course_session, "status": "Published"},
			fields=["name", "title", "caption", "posted_at", "teacher"],
			order_by="posted_at desc, creation desc",
		)
		photo_items = defaultdict(list)
		photo_post_ids = [row.get("name") for row in photo_rows]
		if photo_post_ids and _doctype_available("Session Photo Item"):
			for photo in frappe.get_all(
				"Session Photo Item",
				filters={
					"parent": ["in", photo_post_ids],
					"parenttype": "Session Photo Post",
					"parentfield": "photos",
				},
				fields=["parent", "idx"],
				order_by="parent asc, idx asc",
			):
				photo_items[photo.get("parent")].append({
					"idx": cint(photo.get("idx")),
					"url": _build_school_admin_photo_url(
						course_session,
						photo.get("parent"),
						photo.get("idx"),
						method=photo_method,
					),
				})

		for row in photo_rows:
			teacher_ids.add(row.get("teacher"))
			photos = photo_items.get(row.get("name"), [])
			items.append({
				"type": "photo_post",
				"id": row.get("name"),
				"title": row.get("title") or _("Class Photos"),
				"summary": row.get("caption") or "",
				"published_at": _school_admin_content_datetime(row.get("posted_at")),
				"teacher": row.get("teacher") or "",
				"photo_count": len(photos),
				"photos": photos,
			})

	if _doctype_available("Session Video Post"):
		for row in frappe.get_all(
			"Session Video Post",
			filters={"course_session": course_session, "status": "Published"},
			fields=["name", "title", "caption", "posted_at", "teacher", "file_name", "file_size"],
			order_by="posted_at desc, creation desc",
		):
			teacher_ids.add(row.get("teacher"))
			items.append({
				"type": "video_post",
				"id": row.get("name"),
				"title": row.get("title") or _("Class Video"),
				"summary": row.get("caption") or "",
				"published_at": _school_admin_content_datetime(row.get("posted_at")),
				"teacher": row.get("teacher") or "",
				"file_name": row.get("file_name") or "",
				"file_size": cint(row.get("file_size")),
				"video_url": _build_school_admin_video_url(
					course_session,
					row.get("name"),
					method=video_method,
				),
			})

	teacher_map = {}
	teacher_ids.discard(None)
	teacher_ids.discard("")
	if teacher_ids and _doctype_available("Teacher"):
		teacher_map = {
			row.get("name"): row.get("teacher_name") or row.get("name")
			for row in frappe.get_all(
				"Teacher",
				filters={"name": ["in", sorted(teacher_ids)]},
				fields=["name", "teacher_name"],
				limit_page_length=0,
			)
		}

	for item in items:
		item["teacher_name"] = teacher_map.get(item.get("teacher"), item.get("teacher") or "")

	items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
	return items


def _school_admin_content_datetime(value):
	return str(value) if value else ""


def _build_school_admin_photo_url(
	course_session,
	photo_post,
	photo_idx,
	*,
	method="qas_custom.api.school_admin.school_admin_get_course_session_photo",
):
	return "/api/method/{0}?".format(method) + urlencode({
		"course_session": course_session,
		"photo_post": photo_post,
		"photo_idx": cint(photo_idx),
	})


def _build_school_admin_video_url(
	course_session,
	video_post,
	*,
	method="qas_custom.api.school_admin.school_admin_get_course_session_video",
):
	return "/api/method/{0}?".format(method) + urlencode({
		"course_session": course_session,
		"video_post": video_post,
	})


def _get_school_admin_file_content(file_url, fallback_filename=None, fallback_content_type=None):
	file_doc_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_doc_name:
		raise frappe.DoesNotExistError

	file_doc = frappe.get_doc("File", file_doc_name)
	filename = file_doc.file_name or fallback_filename or file_url.rsplit("/", 1)[-1]
	return {
		"filename": filename,
		"content": file_doc.get_content(),
		"content_type": fallback_content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
	}


def update_school_admin_course_session_teacher_data(course_session=None, teacher=None, reset_override=0):
	_require_school_admin()
	if not course_session:
		frappe.throw(_("Course session is required."))
	doc = frappe.get_doc("Course Sessions", course_session)
	if doc.get("status") != "Scheduled":
		frappe.throw(_("Only scheduled course sessions can have their teacher changed."))
	if cint(reset_override):
		teacher = None
	else:
		_assert_active_teacher(teacher)
	_set_if_field(doc, "teacher_override", teacher)
	doc.save(ignore_permissions=True)
	_add_comment(
		"Course Sessions",
		doc.name,
		"Teacher override reset to weekly timeslot." if not teacher else _("Teacher override changed to {0}.").format(teacher),
	)
	frappe.db.commit()
	return get_school_admin_course_session_data(doc.name)


def change_school_admin_weekly_timeslot_teacher_data(weekly_timeslot=None, teacher=None, effective_date=None):
	_require_school_admin()
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	if not effective_date:
		frappe.throw(_("Effective date is required."))
	effective_date = getdate(effective_date)
	if effective_date < getdate(today()):
		frappe.throw(_("Effective date cannot be before today."))
	_assert_active_teacher(teacher)

	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	previous_teacher = doc.get("teacher")
	if teacher == previous_teacher:
		frappe.throw(_("Choose a different teacher."))

	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": doc.name, "session_date": ["<", effective_date]},
		fields=["name", "teacher_override"],
		limit_page_length=0,
	)
	preserved_count = 0
	for session in sessions:
		if session.get("teacher_override"):
			continue
		frappe.db.set_value("Course Sessions", session.name, "teacher_override", previous_teacher, update_modified=True)
		preserved_count += 1

	future_override_count = frappe.db.count(
		"Course Sessions",
		filters={
			"weekly_timeslot": doc.name,
			"session_date": [">=", effective_date],
			"teacher_override": ["!=", ""],
		},
	)
	doc.teacher = teacher
	doc.save(ignore_permissions=True)
	_add_comment(
		"Weekly Timeslot",
		doc.name,
		_("Teacher changed from {0} to {1} effective {2}. Preserved {3} earlier session(s).").format(
			previous_teacher,
			teacher,
			effective_date,
			preserved_count,
		),
	)
	frappe.db.commit()
	result = get_school_admin_weekly_timeslot_data(doc.name)
	result["teacher_reassignment"] = {
		"previous_teacher": previous_teacher,
		"teacher": teacher,
		"effective_date": str(effective_date),
		"preserved_session_count": preserved_count,
		"future_override_count": future_override_count,
	}
	return result


def update_school_admin_attendance_data(attendance_entry=None, status=None, comments=None):
	_require_school_admin()
	if not attendance_entry:
		frappe.throw(_("Attendance entry is required."))
	row = frappe.get_doc("Class Attendance Entry", attendance_entry)
	result = update_attendance_status(
		course_session=row.course_session,
		attendance_row=row.name,
		status=status,
		actor=frappe.session.user,
		comment=comments,
	)
	frappe.db.commit()
	return result


def create_school_admin_course_session_attendance_data(course_session=None, payload=None):
	_require_school_admin()
	payload = _get_payload(payload)
	if not course_session:
		frappe.throw(_("Course session is required."))
	student = (payload.get("student") or "").strip()
	if not student:
		frappe.throw(_("Student is required."))
	if not frappe.db.exists("Student", student):
		frappe.throw(_("Student was not found."))
	if _has_field("Student", "status"):
		student_status = frappe.db.get_value("Student", student, "status")
		if student_status and student_status != "Active":
			frappe.throw(_("Only active students can be added to a course session."))
	if not frappe.db.exists("Course Sessions", course_session):
		frappe.throw(_("Course session was not found."))
	session = frappe.get_doc("Course Sessions", course_session)
	if session.get("status") == "Cancelled":
		frappe.throw(_("Cannot add attendance to a cancelled course session."))
	enrollment_type = (payload.get("enrollment_type") or "Pay-as-you-go").strip()
	_validate_course_session_attendance_type(enrollment_type)
	status = (payload.get("status") or "To be started").strip()
	_validate_course_session_attendance_status(status)
	comments = (payload.get("comments") or "").strip()
	attendance_entry = create_attendance_entry(
		course_session=course_session,
		student=student,
		enrollment_type=enrollment_type,
		status=status,
		comments=comments or _("Manually added by School Admin."),
		prevent_student_duplicate=True,
	)
	_add_comment(
		"Class Attendance Entry",
		attendance_entry,
		_("Manually added to course session {0} by School Admin.").format(course_session),
	)
	frappe.db.commit()
	return {
		"attendance_entry": attendance_entry,
		"course_session": get_school_admin_course_session_data(course_session),
	}


def _validate_course_session_attendance_type(enrollment_type):
	field = frappe.get_meta("Class Attendance Entry").get_field("enrollment_type")
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	if options and enrollment_type not in options:
		frappe.throw(_("Invalid attendance type: {0}").format(enrollment_type))


def _validate_course_session_attendance_status(status):
	field = frappe.get_meta("Class Attendance Entry").get_field("status")
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	if options and status not in options:
		frappe.throw(_("Invalid attendance status: {0}").format(status))


def _get_school_admin_attendance_rows(course_session, term=None):
	rows = [_docdict(row) for row in get_attendance_entries([course_session])]
	_enrich_trial_confirmation_status(rows)
	teaching_notes_map = _get_student_teaching_notes_map([row.get("student") for row in rows])
	family_map = _get_attendance_family_map([row.get("student") for row in rows])
	outstanding_map = _get_family_outstanding_invoice_map(family_map.values())
	term_invoice_map = _get_family_current_term_invoice_map(family_map.values(), term)
	for row in rows:
		student = row.get("student")
		family = family_map.get(student) or {}
		row["teaching_notes"] = teaching_notes_map.get(student) or ""
		outstanding = outstanding_map.get(family.get("parent")) or {}
		term_invoice = term_invoice_map.get(family.get("parent")) or {}
		row["student_display"] = get_student_display_name(student) or student
		row["student_code"] = get_student_display_code(student) or student
		row["parent_name"] = family.get("parent_name") or ""
		row["parent_phone"] = family.get("parent_phone") or ""
		row["has_outstanding_invoice"] = bool(outstanding.get("amount"))
		row["outstanding_amount"] = flt(outstanding.get("amount") or 0)
		row["has_current_term_invoice"] = bool(term_invoice.get("has_invoice"))
		row["has_current_term_outstanding_invoice"] = bool(term_invoice.get("outstanding_amount"))
		row["current_term_outstanding_amount"] = flt(term_invoice.get("outstanding_amount") or 0)
		row["attendance_type"] = row.get("enrollment_type") or _infer_attendance_type(row)
		row["source_label"] = _attendance_source_label(row)
	return rows


def _enrich_trial_confirmation_status(rows):
	inquiry_names = sorted(
		{
			row.get("source_document")
			for row in rows
			if row.get("source_doctype") == "Inquiry" and row.get("source_document")
		}
	)
	if not inquiry_names:
		return rows

	inquiry_rows = frappe.get_all(
		"Inquiry",
		filters={"name": ["in", inquiry_names]},
		fields=["name", "confirmation_status"],
		limit_page_length=0,
	)
	status_map = {
		row.get("name"): row.get("confirmation_status")
		for row in inquiry_rows
		if row.get("name") and row.get("confirmation_status") in TRIAL_CONFIRMATION_STATUSES
	}
	for row in rows:
		if row.get("source_doctype") == "Inquiry":
			row["trial_confirmation_status"] = status_map.get(row.get("source_document"), "")
	return rows


def _is_visible_course_session_attendance_row(row):
	status = (row.get("status") or "").strip()
	return status not in NON_ATTENDING_ATTENDANCE_STATUSES


def _visible_course_session_attendance_rows(rows):
	return [row for row in rows if _is_visible_course_session_attendance_row(row)]


def _roster_course_session_attendance_rows(rows):
	return list(rows)


def _count_leave_attendance_rows(rows):
	return sum(1 for row in rows if (row.get("status") or "").strip() == "Leave")


def _get_student_teaching_notes_map(student_ids):
	student_ids = sorted({student for student in student_ids if student})
	if not student_ids:
		return {}
	fields = _safe_fields("Student", ["name", "teaching_notes"])
	return {
		row.get("name"): (row.get("teaching_notes") or "").strip()
		for row in frappe.get_all(
			"Student",
			filters={"name": ["in", student_ids]},
			fields=fields,
			limit_page_length=0,
		)
		if row.get("name")
	}


def _get_attendance_family_map(student_ids):
	student_ids = sorted({student for student in student_ids if student})
	parent_field = _student_parent_field()
	if not student_ids or not parent_field:
		return {}

	student_rows = frappe.get_all(
		"Student",
		filters={"name": ["in", student_ids]},
		fields=_safe_fields("Student", ["name", parent_field]),
		limit_page_length=0,
	)
	student_parent_map = {
		row.get("name"): row.get(parent_field)
		for row in student_rows
		if row.get("name")
	}
	parent_ids = sorted({parent for parent in student_parent_map.values() if parent})
	parent_map = _get_roster_parent_map(parent_ids)
	return {
		student: parent_map.get(parent) or {"parent": parent}
		for student, parent in student_parent_map.items()
	}


def _get_roster_parent_map(parent_ids):
	if not parent_ids:
		return {}
	fields = _safe_fields("Parent", ["name", "parent_name", "mobile_number", "phone", "customer"])
	rows = frappe.get_all(
		"Parent",
		filters={"name": ["in", parent_ids]},
		fields=fields,
		limit_page_length=0,
	)
	return {
		row.get("name"): {
			"parent": row.get("name"),
			"parent_name": row.get("parent_name") or row.get("name"),
			"parent_phone": row.get("mobile_number") or row.get("phone") or "",
			"customer": row.get("customer"),
		}
		for row in rows
		if row.get("name")
	}


def _get_family_outstanding_invoice_map(families):
	if not _doctype_available("Sales Invoice"):
		return {}
	parent_ids = sorted({family.get("parent") for family in families if family and family.get("parent")})
	customers = sorted({family.get("customer") for family in families if family and family.get("customer")})
	if not parent_ids and not customers:
		return {}

	customer_parent_map = defaultdict(set)
	for family in families:
		if family and family.get("customer") and family.get("parent"):
			customer_parent_map[family.get("customer")].add(family.get("parent"))

	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "parent", "docstatus", "grand_total", "rounded_total", "outstanding_amount"],
	)
	invoice_rows = []
	if parent_ids and _has_field("Sales Invoice", "parent"):
		invoice_rows.extend(
			frappe.get_all(
				"Sales Invoice",
				filters={"parent": ["in", parent_ids], "docstatus": 1},
				fields=fields,
				limit_page_length=0,
			)
		)
	if customers:
		invoice_rows.extend(
			frappe.get_all(
				"Sales Invoice",
				filters={"customer": ["in", customers], "docstatus": 1},
				fields=fields,
				limit_page_length=0,
			)
		)

	summary = defaultdict(lambda: {"amount": 0})
	seen_invoices = set()
	for row in invoice_rows:
		invoice = row.get("name")
		if not invoice or invoice in seen_invoices:
			continue
		seen_invoices.add(invoice)
		payable = flt(_invoice_credit_payload(row).get("payable_amount") or 0)
		if payable <= 0:
			continue
		target_parents = {row.get("parent")} if row.get("parent") else customer_parent_map.get(row.get("customer"), set())
		for parent in target_parents:
			if parent:
				summary[parent]["amount"] += payable
	return summary


def _get_family_current_term_invoice_map(families, term):
	if not term or not _doctype_available("Sales Invoice"):
		return {}
	parent_ids = sorted({family.get("parent") for family in families if family and family.get("parent")})
	customers = sorted({family.get("customer") for family in families if family and family.get("customer")})
	if not parent_ids and not customers:
		return {}

	invoice_names = set()
	if _has_field("Sales Invoice", "term"):
		invoice_names.update(
			frappe.get_all(
				"Sales Invoice",
				filters={"term": term, "docstatus": 1},
				pluck="name",
				limit_page_length=0,
			)
		)
	if _doctype_available("Sales Invoice Item") and _has_field("Sales Invoice Item", "term"):
		item_filters = {"term": term}
		if frappe.db.has_column("Sales Invoice Item", "parenttype"):
			item_filters["parenttype"] = "Sales Invoice"
		invoice_names.update(
			frappe.get_all(
				"Sales Invoice Item",
				filters=item_filters,
				pluck="parent",
				limit_page_length=0,
			)
		)
	if not invoice_names:
		return {}

	customer_parent_map = defaultdict(set)
	for family in families:
		if family and family.get("customer") and family.get("parent"):
			customer_parent_map[family.get("customer")].add(family.get("parent"))

	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "parent", "docstatus", "grand_total", "rounded_total", "outstanding_amount"],
	)
	invoice_rows = []
	if parent_ids and _has_field("Sales Invoice", "parent"):
		invoice_rows.extend(
			frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", sorted(invoice_names)], "parent": ["in", parent_ids], "docstatus": 1},
				fields=fields,
				limit_page_length=0,
			)
		)
	if customers:
		invoice_rows.extend(
			frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", sorted(invoice_names)], "customer": ["in", customers], "docstatus": 1},
				fields=fields,
				limit_page_length=0,
			)
		)

	summary = defaultdict(lambda: {"has_invoice": False, "outstanding_amount": 0})
	seen_invoices = set()
	for row in invoice_rows:
		invoice = row.get("name")
		if not invoice or invoice in seen_invoices:
			continue
		seen_invoices.add(invoice)
		target_parents = {row.get("parent")} if row.get("parent") else customer_parent_map.get(row.get("customer"), set())
		payable = flt(_invoice_credit_payload(row).get("payable_amount") or 0)
		for parent in target_parents:
			if not parent:
				continue
			summary[parent]["has_invoice"] = True
			summary[parent]["outstanding_amount"] += payable
	return summary


def _infer_attendance_type(row):
	source_doctype = row.get("source_doctype")
	if source_doctype == "Inquiry":
		return "Trial"
	if source_doctype == "Makeup Voucher" or row.get("makeup_voucher"):
		return "Makeup"
	return "Full-Term"


def _attendance_source_label(row):
	source_doctype = row.get("source_doctype")
	source_document = row.get("source_document")
	if source_doctype and source_document:
		return f"{source_doctype} {source_document}"
	if row.get("makeup_voucher"):
		return f"Makeup Voucher {row.get('makeup_voucher')}"
	return ""


def get_school_admin_vouchers_data(student=None, status=None, limit=120):
	_require_school_admin()
	if not _doctype_available("Makeup Voucher"):
		return {"items": []}
	filters = {}
	if student:
		filters["student"] = student
	if status:
		filters["status"] = status
	fields = _safe_fields(
		"Makeup Voucher",
		["name", "student", "course", "original_session", "leave_request", "status", "issue_date", "expiry_date", "used_on_session", "used_date", "used_by_student", "voucher_label"],
	)
	rows = frappe.get_all(
		"Makeup Voucher",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=_limit(limit, default=120, max_value=300),
	)
	return {"items": [_normalize_row_payload("Makeup Voucher", row) for row in rows]}



def get_school_admin_leave_options_data(parent=None, student=None):
    _require_school_admin()
    if not student:
        frappe.throw(_("Student is required."))
    parent_doc, students = _get_school_admin_family_context(parent=parent, student=student)
    _assert_student_in_family(student, students)
    return {"parent": parent_doc.name, "student": student, "sessions": _get_school_admin_leave_sessions(student)}


def submit_school_admin_leave_request_data(parent=None, student=None, course_session=None, reason=None):
    _require_school_admin()
    reason = _school_admin_required_reason(reason)
    if not course_session:
        frappe.throw(_("Course session is required."))
    parent_doc, students = _get_school_admin_family_context(parent=parent, student=student)
    _assert_student_in_family(student, students)
    eligible_sessions = _get_school_admin_leave_sessions(student, course_session=course_session)
    if not eligible_sessions:
        frappe.throw(_("This class session is not eligible for School Admin leave in the current active term."))
    selected_session = eligible_sessions[0]
    result = submit_parent_leave_request_core(
        parent=parent_doc,
        students=students,
        student=student,
        course_session=course_session,
        allowed_attendance_statuses=SCHOOL_ADMIN_LEAVE_ATTENDANCE_STATUSES,
        allow_started_session=True,
        notify_staff=not selected_session.get("is_past"),
        attendance_entry=selected_session.get("attendance_entry"),
    )
    _audit_school_admin_leave_result(result, reason)
    frappe.db.commit()
    return result


def get_school_admin_redeemable_sessions_data(parent=None, voucher_id=None, student=None):
    _require_school_admin()
    parent_doc, students, voucher = _get_school_admin_voucher_family_context(parent=parent, voucher_id=voucher_id)
    if student:
        _assert_student_in_family(student, students)
    return get_parent_redeemable_sessions_core(
        parent=parent_doc,
        students=students,
        voucher_id=voucher.name,
        student=student,
    )


def redeem_school_admin_voucher_data(parent=None, voucher_id=None, session_id=None, student=None, reason=None):
    _require_school_admin()
    reason = _school_admin_required_reason(reason)
    parent_doc, students, voucher = _get_school_admin_voucher_family_context(parent=parent, voucher_id=voucher_id)
    _assert_student_in_family(student, students)
    result = redeem_parent_voucher_core(
        parent=parent_doc,
        students=students,
        voucher_id=voucher.name,
        session_id=session_id,
        student=student,
    )
    _audit_school_admin_redeem_result(result, reason)
    frappe.db.commit()
    return result


def cancel_school_admin_makeup_booking_data(parent=None, voucher_id=None, reason=None, confirm_cancel=0):
    _require_school_admin()
    _parent_doc, _students, voucher = _get_school_admin_voucher_family_context(parent=parent, voucher_id=voucher_id)
    reason = str(reason or "").strip()
    try:
        result = cancel_makeup_booking_core(voucher, confirm_cancel=confirm_cancel)
        _audit_school_admin_makeup_cancellation_result(result, reason)
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        raise


def _get_school_admin_family_context(parent=None, student=None):
    context = _resolve_family_context(parent=parent, student=student)
    parent_id = context.get("parent")
    if not parent_id:
        frappe.throw(_("Family parent was not found."))
    if parent and parent_id != parent:
        frappe.throw(_("Selected student does not belong to this family."), frappe.PermissionError)
    parent_doc = frappe.get_doc("Parent", parent_id)
    students = _get_family_students(parent_id)
    if not students:
        frappe.throw(_("This family has no linked students."))
    return parent_doc, students


def _get_school_admin_voucher_family_context(parent=None, voucher_id=None):
    if not voucher_id:
        frappe.throw(_("Makeup voucher is required."))
    if not frappe.db.exists("Makeup Voucher", voucher_id):
        frappe.throw(_("Makeup voucher was not found."))
    voucher = frappe.get_doc("Makeup Voucher", voucher_id)
    if not voucher.get("student"):
        frappe.throw(_("This makeup voucher is missing a source student."))
    source_parent = _find_parent_for_student(voucher.student)
    if parent and source_parent and parent != source_parent:
        frappe.throw(_("This voucher does not belong to the selected family."), frappe.PermissionError)
    parent_doc, students = _get_school_admin_family_context(parent=parent or source_parent, student=voucher.student)
    return parent_doc, students, voucher


def _assert_student_in_family(student, students):
    if not student:
        frappe.throw(_("Student is required."))
    allowed = {row.get("name") for row in students}
    if student not in allowed:
        frappe.throw(_("This student is not linked to the selected family."), frappe.PermissionError)
    return student


def _school_admin_required_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Reason is required."))
    return reason


def _get_school_admin_leave_sessions(student, course_session=None):
    required_doctypes = ("Class Attendance Entry", "Course Sessions", "Enrollment", "Term")
    if any(not _doctype_available(doctype) for doctype in required_doctypes):
        return []
    fields = _safe_fields(
        "Class Attendance Entry",
        ["name", "course_session", "student", "status", "enrollment_type", "source_doctype", "source_document"],
    )
    attendance_filters = {
        "student": student,
        "status": ["in", list(SCHOOL_ADMIN_LEAVE_ATTENDANCE_STATUSES)],
        "source_doctype": "Enrollment",
    }
    if course_session:
        attendance_filters["course_session"] = course_session
    attendance_rows = frappe.get_all(
        "Class Attendance Entry",
        filters=attendance_filters,
        fields=fields,
        limit_page_length=0,
    )
    if not attendance_rows:
        return []

    enrollment_names = sorted({row.get("source_document") for row in attendance_rows if row.get("source_document")})
    enrollment_rows = frappe.get_all(
        "Enrollment",
        filters={"name": ["in", enrollment_names]},
        fields=_safe_fields("Enrollment", ["name", "student", "term", "weekly_timeslot", "enrollment_type", "status"]),
        limit_page_length=0,
    ) if enrollment_names else []
    term_names = sorted({row.get("term") for row in enrollment_rows if row.get("term")})
    term_rows = frappe.get_all(
        "Term",
        filters={"name": ["in", term_names]},
        fields=_safe_fields("Term", ["name", "term_name", "start_date", "end_date", "status"]),
        limit_page_length=0,
    ) if term_names else []
    session_ids = sorted({row.get("course_session") for row in attendance_rows if row.get("course_session")})
    if not session_ids:
        return []
    session_rows = frappe.get_all(
        "Course Sessions",
        filters={"name": ["in", session_ids]},
        fields=_safe_fields("Course Sessions", ["name", "weekly_timeslot", "session_date", "status"]),
        order_by="session_date asc, modified asc",
        limit_page_length=0,
    )
    timeslot_map = _get_timeslot_map([row.get("weekly_timeslot") for row in session_rows if row.get("weekly_timeslot")])
    sessions = _build_school_admin_leave_session_options(
        attendance_rows=attendance_rows,
        enrollment_rows=enrollment_rows,
        term_rows=term_rows,
        session_rows=session_rows,
        timeslot_map=timeslot_map,
        current_datetime=now_datetime(),
    )
    return [
        row for row in sessions
        if not _school_admin_has_active_leave_or_voucher(student, row.get("session_id"))
    ]


def _build_school_admin_leave_session_options(
    *,
    attendance_rows,
    enrollment_rows,
    term_rows,
    session_rows,
    timeslot_map,
    current_datetime,
):
    enrollment_map = {row.get("name"): row for row in enrollment_rows if row.get("name")}
    term_map = {row.get("name"): row for row in term_rows if row.get("name")}
    session_map = {row.get("name"): row for row in session_rows if row.get("name")}
    options_by_session = {}

    for attendance in attendance_rows:
        if attendance.get("status") not in SCHOOL_ADMIN_LEAVE_ATTENDANCE_STATUSES:
            continue
        if attendance.get("source_doctype") != "Enrollment" or attendance.get("enrollment_type") == "Makeup":
            continue
        enrollment = enrollment_map.get(attendance.get("source_document"))
        if not enrollment or enrollment.get("status") != "Active" or enrollment.get("enrollment_type") != "Full-Term":
            continue
        if enrollment.get("student") and attendance.get("student") != enrollment.get("student"):
            continue
        term = term_map.get(enrollment.get("term"))
        if not term or term.get("status") != "Active" or not term.get("start_date") or not term.get("end_date"):
            continue
        session = session_map.get(attendance.get("course_session"))
        if not session or session.get("status") == "Cancelled" or not session.get("session_date"):
            continue
        session_date = getdate(session.get("session_date"))
        if session_date < getdate(term.get("start_date")) or session_date > getdate(term.get("end_date")):
            continue
        if enrollment.get("weekly_timeslot") and session.get("weekly_timeslot") != enrollment.get("weekly_timeslot"):
            continue
        timeslot = timeslot_map.get(session.get("weekly_timeslot"))
        if not timeslot or not timeslot.get("start_time"):
            continue
        session_id = session.get("name")
        if not session_id or session_id in options_by_session:
            continue
        session_start = _school_admin_session_start(session, timeslot)
        options_by_session[session_id] = {
            "session_id": session_id,
            "course": timeslot.get("course"),
            "session_date": session.get("session_date"),
            "day_of_week": timeslot.get("day_of_week"),
            "start_time": timeslot.get("start_time"),
            "end_time": timeslot.get("end_time"),
            "campus": timeslot.get("campus"),
            "classroom": timeslot.get("classroom"),
            "teacher": timeslot.get("teacher"),
            "attendance_entry": attendance.get("name"),
            "attendance_status": attendance.get("status"),
            "term": term.get("name"),
            "term_label": term.get("term_name") or term.get("name"),
            "is_past": session_start <= current_datetime,
            "_session_start": session_start,
        }

    sessions = sorted(options_by_session.values(), key=lambda row: row.get("_session_start"))
    for row in sessions:
        row.pop("_session_start", None)
    return sessions


def _school_admin_session_start(session, timeslot):
    if not session.get("session_date") or not timeslot.get("start_time"):
        frappe.throw(_("The selected class session is missing date or time."))
    return datetime.combine(getdate(session.get("session_date")), get_time(timeslot.get("start_time")))


def _school_admin_has_active_leave_or_voucher(student, course_session):
    if frappe.db.exists("Leave Request", {"student": student, "course_session": course_session, "status": "Approved"}):
        return True
    return bool(frappe.db.exists(
        "Makeup Voucher",
        {
            "student": student,
            "original_session": course_session,
            "status": ["in", ["Valid", "Used"]],
        },
    ))


def _get_family_voucher_rows(students=None, status=None, limit=80):
    if not students or not _doctype_available("Makeup Voucher"):
        return []
    filters = {"student": ["in", students]}
    if status:
        filters["status"] = status
    fields = _safe_fields(
        "Makeup Voucher",
        ["name", "student", "course", "original_session", "leave_request", "status", "issue_date", "expiry_date", "used_on_session", "used_date", "used_by_student", "voucher_label"],
    )
    rows = frappe.get_all(
        "Makeup Voucher",
        filters=filters,
        fields=fields,
        order_by="modified desc",
        limit=_limit(limit, default=80, max_value=300),
    )
    items = [_normalize_row_payload("Makeup Voucher", row) for row in rows]
    attendance_map = _get_family_makeup_attendance_map(items)
    session_map = _get_school_admin_session_summary_map(
        [item.get("original_session") for item in items if item.get("original_session")]
        + [item.get("used_on_session") for item in items if item.get("used_on_session")]
    )
    for item in items:
        item["voucher_id"] = item.get("name")
        item["voucher_label"] = get_makeup_voucher_label(item)
        item["student_display"] = get_student_display_name(item.get("student")) or item.get("student")
        item["source_student_display"] = item["student_display"]
        item["used_by_student_display"] = get_student_display_name(item.get("used_by_student")) if item.get("used_by_student") else None
        original_session = session_map.get(item.get("original_session")) or {}
        used_session = session_map.get(item.get("used_on_session")) or {}
        item["leave_session_date"] = original_session.get("session_date")
        item["leave_day_of_week"] = original_session.get("day_of_week")
        item["leave_start_time"] = original_session.get("start_time")
        item["used_session_date"] = used_session.get("session_date")
        item["used_day_of_week"] = used_session.get("day_of_week")
        item["used_start_time"] = used_session.get("start_time")
        attendance = attendance_map.get(item.get("name")) or {}
        item["makeup_attendance_entry"] = attendance.get("name")
        item["makeup_attendance_status"] = attendance.get("status")
    return items


def _get_family_makeup_attendance_map(vouchers):
    used_vouchers = [row for row in vouchers if row.get("status") == "Used" and row.get("used_on_session")]
    voucher_names = [row.get("name") for row in used_vouchers if row.get("name")]
    if not voucher_names or not _doctype_available(ATTENDANCE_DOCTYPE):
        return {}

    linked_rows = []
    for row in frappe.get_all(
        ATTENDANCE_DOCTYPE,
        filters={"source_doctype": "Makeup Voucher", "source_document": ["in", voucher_names]},
        fields=["name", "course_session", "student", "status", "source_document"],
        limit_page_length=0,
    ):
        linked_rows.append({**row, "voucher": row.get("source_document")})
    if frappe.db.has_column(ATTENDANCE_DOCTYPE, "makeup_voucher"):
        for row in frappe.get_all(
            ATTENDANCE_DOCTYPE,
            filters={"makeup_voucher": ["in", voucher_names]},
            fields=["name", "course_session", "student", "status", "makeup_voucher"],
            limit_page_length=0,
        ):
            linked_rows.append({**row, "voucher": row.get("makeup_voucher")})

    result = {}
    for voucher in used_vouchers:
        student = voucher.get("used_by_student") or voucher.get("student")
        matches_by_name = {
            row.get("name"): row
            for row in linked_rows
            if row.get("voucher") == voucher.get("name")
            and row.get("course_session") == voucher.get("used_on_session")
            and row.get("student") == student
        }
        if len(matches_by_name) == 1:
            result[voucher.get("name")] = next(iter(matches_by_name.values()))
    return result


def _get_school_admin_session_summary_map(session_ids):
    session_ids = sorted({session_id for session_id in session_ids if session_id})
    if not session_ids:
        return {}
    rows = frappe.get_all(
        "Course Sessions",
        filters={"name": ["in", session_ids]},
        fields=_safe_fields("Course Sessions", ["name", "weekly_timeslot", "session_date", "status"]),
        limit_page_length=0,
    )
    timeslot_map = _get_timeslot_map([row.get("weekly_timeslot") for row in rows if row.get("weekly_timeslot")])
    payload = {}
    for row in rows:
        timeslot = timeslot_map.get(row.get("weekly_timeslot")) or {}
        payload[row.get("name")] = {
            "session_id": row.get("name"),
            "course": timeslot.get("course"),
            "session_date": row.get("session_date"),
            "day_of_week": timeslot.get("day_of_week"),
            "start_time": timeslot.get("start_time"),
            "end_time": timeslot.get("end_time"),
            "campus": timeslot.get("campus"),
            "classroom": timeslot.get("classroom"),
            "teacher": timeslot.get("teacher"),
        }
    return payload


def _audit_school_admin_leave_result(result, reason):
    user = frappe.session.user
    leave_request = result.get("leave_request")
    voucher = result.get("makeup_voucher")
    session = result.get("session") or {}
    comment = _("Created by School Admin {0}. Reason: {1}").format(user, reason)
    if leave_request:
        _add_comment("Leave Request", leave_request, comment)
    if voucher:
        _add_comment("Makeup Voucher", voucher, _("Generated from School Admin leave request by {0}. Reason: {1}").format(user, reason))
    attendance_entry = frappe.db.get_value(
        "Class Attendance Entry",
        {"course_session": session.get("session_id"), "student": session.get("student"), "status": "Leave"},
        "name",
        order_by="modified desc",
    )
    if attendance_entry:
        _add_comment("Class Attendance Entry", attendance_entry, comment)


def _audit_school_admin_redeem_result(result, reason):
    user = frappe.session.user
    voucher = (result.get("voucher") or {}).get("voucher_id")
    attendance_entry = result.get("attendance_entry")
    if voucher:
        _add_comment("Makeup Voucher", voucher, _("Redeemed by School Admin {0}. Reason: {1}").format(user, reason))
    if attendance_entry:
        _add_comment("Class Attendance Entry", attendance_entry, _("Created from School Admin voucher redemption by {0}. Reason: {1}").format(user, reason))


def _audit_school_admin_makeup_cancellation_result(result, reason):
    user = frappe.session.user
    voucher = (result.get("voucher") or {}).get("voucher_id")
    attendance = result.get("attendance") or {}
    attendance_entry = attendance.get("attendance_entry")
    status_before = attendance.get("status_before") or _("blank")
    comment = _("Makeup booking cancelled by School Admin {0}. Previous attendance status: {1}.").format(user, status_before)
    if reason:
        comment = _("{0} Reason: {1}").format(comment, reason)
    if voucher:
        _add_comment("Makeup Voucher", voucher, comment)
    if attendance_entry:
        _add_comment("Class Attendance Entry", attendance_entry, comment)


def update_school_admin_voucher_data(voucher=None, payload=None):
	_require_school_admin()
	if not voucher:
		frappe.throw(_("Voucher is required."))
	payload = _get_payload(payload)
	doc = frappe.get_doc("Makeup Voucher", voucher)
	for fieldname in ["status", "expiry_date", "used_on_session", "used_date", "used_by_student"]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _document_payload(doc)


def get_school_admin_teacher_revenue_share_sessions_data(
	from_date=None,
	to_date=None,
	teacher=None,
	campus=None,
	course=None,
	owned_only=1,
	limit=200,
):
	_require_school_admin()
	return {
		"items": get_teacher_revenue_share_session_rows(
			from_date=from_date,
			to_date=to_date,
			teacher=teacher,
			campus=campus,
			course=course,
			owned_only=owned_only,
			limit=limit,
		)
	}


def _require_school_admin():
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can access School Admin APIs."), frappe.PermissionError)


def _get_payload(payload=None):
	if payload is None:
		payload = frappe.form_dict.get("payload")
	if isinstance(payload, str):
		return json.loads(payload) if payload.strip() else {}
	return payload or {}


def _limit(value, default=80, max_value=200):
	value = cint(value or default)
	if value <= 0:
		value = default
	return min(value, max_value)


def _count(doctype, filters):
	if not _doctype_available(doctype):
		return 0
	try:
		return frappe.db.count(doctype, filters)
	except Exception:
		return 0


def _get_active_enrollment_counts_for_timeslots(weekly_timeslots):
	weekly_timeslots = [row for row in weekly_timeslots if row]
	if not weekly_timeslots or not _doctype_available("Enrollment") or not _has_field("Enrollment", "weekly_timeslot"):
		return {}
	fields = ["weekly_timeslot", "count(name) as active_count"]
	filters = {"weekly_timeslot": ["in", weekly_timeslots]}
	if _has_field("Enrollment", "status"):
		filters["status"] = "Active"
	rows = frappe.get_all(
		"Enrollment",
		filters=filters,
		fields=fields,
		group_by="weekly_timeslot",
		limit_page_length=0,
	)
	return {row.get("weekly_timeslot"): cint(row.get("active_count")) for row in rows}


def _get_weekly_timeslot_reference_options():
	return {
		"courses": _get_link_options("Course", label_fields=["course_name"], filters={"status": "Active"}),
		"campuses": _get_link_options("Campus", label_fields=["campus_name"]),
		"classrooms": _get_link_options("Classroom", label_fields=["classroom_name", "room_name"]),
		"teachers": _get_link_options("Teacher", label_fields=["teacher_name", "teacher_full_name"]),
	}


def _get_link_options(doctype, label_fields=None, filters=None, limit=500):
	if not _doctype_available(doctype):
		return []
	label_fields = label_fields or []
	field_candidates = ["name", *label_fields, "status"]
	if doctype == "Course":
		field_candidates = [*field_candidates, *COURSE_LABEL_FIELDS, "duration_mins"]
	if doctype == "Classroom":
		field_candidates = [*field_candidates, "campus"]
	fields = _safe_fields(doctype, field_candidates)
	active_filters = {}
	if filters:
		for fieldname, value in filters.items():
			if _has_field(doctype, fieldname):
				active_filters[fieldname] = value
	rows = frappe.get_all(
		doctype,
		filters=active_filters,
		fields=fields,
		order_by="name asc",
		limit=_limit(limit, default=500, max_value=1000),
	)
	items = []
	for row in rows:
		label = next((row.get(fieldname) for fieldname in label_fields if row.get(fieldname)), None) or row.get("name")
		if doctype == "Classroom":
			label = _classroom_display_label(row)
		item = {"value": row.get("name"), "label": label}
		if doctype == "Course":
			_attach_course_label(item, row.get("name"), row)
			item["duration_mins"] = cint(row.get("duration_mins"))
		items.append(item)
	return items


def _course_label_map(course_names):
	course_names = sorted({course for course in course_names if course})
	if not course_names or not _doctype_available("Course"):
		return {}
	fields = _safe_fields("Course", COURSE_LABEL_FIELDS)
	rows = frappe.get_all("Course", filters={"name": ["in", course_names]}, fields=fields, limit_page_length=0)
	return {row.get("name"): _docdict(row) for row in rows}


def _attach_course_labels(items, fieldname="course"):
	label_map = _course_label_map([item.get(fieldname) for item in items])
	for item in items:
		_attach_course_label(item, item.get(fieldname), label_map.get(item.get(fieldname)))
	return items


def _attach_course_label(item, course, course_row=None):
	if not course:
		return item
	course_row = _docdict(course_row) if course_row else {}
	label_en = course_row.get("course_name") or course
	label_zh = course_row.get("course_name_zh") or label_en
	item["course_label"] = label_en
	item["course_label_en"] = label_en
	item["course_label_zh"] = label_zh
	return item


def _school_setup_config(record_type):
	key = (record_type or "").strip().lower()
	if key not in SCHOOL_SETUP_TYPES:
		frappe.throw(_("Unsupported school setup type: {0}").format(record_type or ""))
	return SCHOOL_SETUP_TYPES[key]


def _school_setup_payload(config, row):
	payload = _normalize_row_payload(config["doctype"], row)
	title_field = config["title_field"]
	payload["record_type"] = next((key for key, value in SCHOOL_SETUP_TYPES.items() if value["doctype"] == config["doctype"]), "")
	payload["label"] = payload.get(title_field) or payload.get("name")
	if config["doctype"] == "Classroom":
		payload["display_label"] = _classroom_display_label(payload)
	return payload


def _normalize_school_setup_record(doc, config):
	if _has_field(doc.doctype, "status"):
		status = doc.get("status")
		if not status:
			doc.set("status", "Active")
		elif status not in {"Active", "Inactive"}:
			frappe.throw(_("Status must be Active or Inactive."))
	if doc.doctype == "Classroom":
		_normalize_classroom_record(doc)


def _normalize_classroom_record(doc):
	classroom_name = (doc.get("classroom_name") or "").strip()
	campus = (doc.get("campus") or "").strip()
	doc.set("classroom_name", classroom_name)
	doc.set("campus", campus)
	if campus and not frappe.db.exists("Campus", campus):
		frappe.throw(_("Campus does not exist: {0}").format(campus))
	if not classroom_name or not campus:
		return
	duplicate_filters = {
		"campus": campus,
		"classroom_name": classroom_name,
		"name": ["!=", doc.get("name")],
	}
	if frappe.db.exists("Classroom", duplicate_filters):
		frappe.throw(_("Room {0} already exists at {1}.").format(classroom_name, campus))
	if _doc_is_new(doc):
		doc.name = _classroom_record_name(campus, classroom_name)


def _doc_is_new(doc):
	try:
		return bool(doc.is_new())
	except Exception:
		return not bool(doc.get("name"))


def _classroom_record_name(campus, classroom_name):
	return "-".join([_slug_part(campus), _slug_part(classroom_name)])


def _slug_part(value):
	text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
	return text or "room"


def _classroom_display_label(row):
	classroom_name = row.get("classroom_name") or row.get("room_name") or row.get("name") or ""
	campus = row.get("campus") or ""
	return " · ".join([part for part in [campus, classroom_name] if part])



def _matching_student_parent_ids(query):
	query = (query or "").strip()
	if not query or not _doctype_available("Student") or not _doctype_available("Parent"):
		return []
	parent_fields = [fieldname for fieldname in ["guardian", "parent"] if _has_field("Student", fieldname)]
	if not parent_fields:
		return []
	student_filters = _text_search_filters("Student", query, ["name", "student_name", "first_name", "last_name"])
	if not student_filters:
		return []
	fields = ["name", *parent_fields]
	try:
		students = frappe.get_all("Student", or_filters=student_filters, fields=fields, limit=50)
	except Exception:
		return []
	parent_ids = []
	for row in students:
		for fieldname in parent_fields:
			if row.get(fieldname):
				parent_ids.append(row.get(fieldname))
	return sorted(set(parent_ids))


def _text_search_filters(doctype, query, fields):
	query = (query or "").strip()
	if not query:
		return None
	search_fields = [fieldname for fieldname in fields if fieldname == "name" or _has_field(doctype, fieldname)]
	return [[doctype, fieldname, "like", f"%{query}%"] for fieldname in search_fields] or None


def _apply_master_payload(doc, payload, candidate_fields):
	payload = payload or {}
	for fieldname in candidate_fields:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))


def _normalize_student_teaching_notes(payload):
	if payload is not None and "teaching_notes" in payload:
		payload["teaching_notes"] = str(payload.get("teaching_notes") or "").strip()


def _validate_required(doc, required_fields):
	for fieldname in required_fields:
		if _has_field(doc.doctype, fieldname) and not doc.get(fieldname):
			frappe.throw(_("{0} is required.").format(fieldname.replace('_', ' ').title()))


def _set_student_parent(doc, parent):
	if not parent:
		return
	if not frappe.db.exists("Parent", parent):
		frappe.throw(_("Parent does not exist: {0}").format(parent))
	for fieldname in ["guardian", "parent"]:
		if _has_field("Student", fieldname):
			_set_if_field(doc, fieldname, parent)
			return


def _get_course_payload(course):
	fields = _safe_fields("Course", ["name", *COURSE_EDIT_FIELDS, "modified"])
	rows = frappe.get_all("Course", filters={"name": course}, fields=fields, limit=1)
	if not rows:
		return {"doctype": "Course", "name": course}
	payload = _normalize_row_payload("Course", rows[0])
	_attach_course_label(payload, payload.get("name"), payload)
	return payload


def _assert_safe_delete(doctype, name):
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} was not found.").format(doctype))
	references = _delete_reference_checks(doctype, name)
	if references:
		frappe.throw(_("Cannot delete {0} because linked business records exist: {1}. Archive or mark inactive instead.").format(doctype, ", ".join(references)))


def _assert_safe_delete_enrollment(doc):
	if doc.get("status") != "Planned":
		frappe.throw(_("Only planned enrollments can be deleted. Cancel or end this enrollment instead."))

	references = []
	if doc.get("start_course_session"):
		references.append(_("start session"))
	if doc.get("invoice"):
		references.append(_("invoice"))
	if doc.get("invoice_status") or flt(doc.get("invoice_amount")):
		references.append(_("billing snapshot"))
	if doc.get("source_inquiry"):
		references.append(_("source inquiry"))

	if _linked_record_exists("Class Attendance Entry", {"source_doctype": "Enrollment", "source_document": doc.name}):
		references.append(_("attendance"))
	if _active_sales_invoice_item_exists_for_enrollment(doc.name):
		references.append(_("invoice item"))
	if _linked_record_exists("Sales Invoice", {"source_doctype": "Enrollment", "source_document": doc.name}):
		references.append(_("sales invoice"))

	if references:
		frappe.throw(_("Cannot delete this planned enrollment because linked business records exist: {0}. Cancel it instead.").format(", ".join(references)))


def _linked_record_exists(doctype, filters):
	if not _doctype_available(doctype):
		return False
	for fieldname in filters:
		if not _has_field(doctype, fieldname):
			return False
	return bool(frappe.db.exists(doctype, filters))


def _active_sales_invoice_item_exists_for_enrollment(enrollment):
	return bool(_active_sales_invoice_for_enrollment_item(enrollment))


def _active_sales_invoice_for_enrollment_item(enrollment):
	if not enrollment or not _doctype_available("Sales Invoice Item") or not _has_field("Sales Invoice Item", "enrollment"):
		return None
	if not _doctype_available("Sales Invoice"):
		return None

	filters = {"enrollment": enrollment}
	if frappe.db.has_column("Sales Invoice Item", "parenttype"):
		filters["parenttype"] = "Sales Invoice"
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters=filters,
		fields=["name", "parent"],
		limit_page_length=20,
	)
	invoice_names = [row.get("parent") for row in rows if row.get("parent")]
	if not invoice_names:
		return None

	return _active_sales_invoice_name({"name": ["in", invoice_names]})


def _active_sales_invoice_filters(filters):
	filters = dict(filters or {})
	filters["docstatus"] = ["!=", 2]
	if _has_field("Sales Invoice", "status"):
		filters["status"] = ["!=", "Cancelled"]
	return filters


def _active_sales_invoice_name(filters):
	if not _doctype_available("Sales Invoice"):
		return None
	rows = frappe.get_all(
		"Sales Invoice",
		filters=_active_sales_invoice_filters(filters),
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None


def _existing_invoice_for_enrollment(enrollment_doc):
	invoice = enrollment_doc.get("invoice")
	if invoice:
		active_invoice = _active_sales_invoice_name({"name": invoice})
		if active_invoice:
			return active_invoice
	if not _doctype_available("Sales Invoice"):
		return None
	if _has_field("Sales Invoice", "enrollment"):
		active_invoice = _active_sales_invoice_name({"enrollment": enrollment_doc.name})
		if active_invoice:
			return active_invoice
	if _has_field("Sales Invoice", "source_doctype") and _has_field("Sales Invoice", "source_document"):
		active_invoice = _active_sales_invoice_name({"source_doctype": "Enrollment", "source_document": enrollment_doc.name})
		if active_invoice:
			return active_invoice
	item_invoice = _active_sales_invoice_for_enrollment_item(enrollment_doc.name)
	if item_invoice:
		return item_invoice
	return None


def _delete_reference_checks(doctype, name):
	checks = []
	if doctype == "Parent":
		student_parent_fields = [fieldname for fieldname in ["guardian", "parent"] if _has_field("Student", fieldname)]
		for field in student_parent_fields:
			checks.append(("Student", field))
		checks.extend([
			("Enrollment", "parent"),
			("Inquiry", "parent"),
			("Sales Invoice", "parent"),
			("QAS Store Credit Ledger", "parent"),
		])
	elif doctype == "Student":
		checks = [("Enrollment", "student"), ("Inquiry", "student"), ("Class Attendance Entry", "student"), ("Makeup Voucher", "student"), ("Makeup Voucher", "used_by_student"), ("Sales Invoice Item", "student"), ("Leave Request", "student")]
	elif doctype == "Course":
		checks = [("Weekly Timeslot", "course"), ("Enrollment", "course"), ("Inquiry", "preferred_course"), ("Makeup Voucher", "course"), ("Sales Invoice Item", "course"), ("Leave Request", "course")]
	elif doctype == "Term":
		checks = [("Weekly Timeslot", "term"), ("Enrollment", "term"), ("Sales Invoice Item", "term")]
	elif doctype == "Campus":
		checks = [("Classroom", "campus"), ("Weekly Timeslot", "campus"), ("Course Sessions", "campus"), ("Inquiry", "campus")]
	elif doctype == "Classroom":
		checks = [("Weekly Timeslot", "classroom"), ("Course Sessions", "classroom")]
	elif doctype == "Teacher":
		checks = [("Weekly Timeslot", "teacher"), ("Course Sessions", "teacher")]
	found = []
	for target_doctype, fieldname in checks:
		if not fieldname or not _doctype_available(target_doctype) or not _has_field(target_doctype, fieldname):
			continue
		try:
			if frappe.db.count(target_doctype, {fieldname: name}):
				found.append(target_doctype)
		except Exception:
			continue
	return sorted(set(found))

def _is_truthy(value):
	return str(value).lower() in {"1", "true", "yes", "y"}


def _active_term_names():
	if not _doctype_available("Term") or not _has_field("Term", "status"):
		return None
	return frappe.get_all(
		"Term",
		filters={"status": ["in", ACTIVE_TERM_STATUSES]},
		pluck="name",
		limit_page_length=0,
	)


def _apply_active_term_filter(filters, term=None, include_inactive_terms=0):
	if term or _is_truthy(include_inactive_terms):
		return
	active_terms = _active_term_names()
	if active_terms is None:
		return
	filters["term"] = ["in", active_terms or ["__qas_no_active_term__"]]


def _parse_status_list(value):
	if not value:
		return []
	if isinstance(value, str):
		return [item.strip() for item in value.split(",") if item.strip()]
	if isinstance(value, (list, tuple, set)):
		return [str(item).strip() for item in value if str(item).strip()]
	return [str(value).strip()]


def _apply_active_timeslot_filter(filters, include_inactive_timeslots=0):
	if _is_truthy(include_inactive_timeslots):
		return
	if _has_field("Weekly Timeslot", "status"):
		filters["status"] = ["in", ACTIVE_TIMESLOT_STATUSES]


def _count_sales_invoices(filters):
	if not _doctype_available("Sales Invoice"):
		return 0
	return _count("Sales Invoice", filters)


def _get_outstanding_invoice_summary():
	if not _doctype_available("Sales Invoice"):
		return {"count": 0, "amount": 0}

	filters = {"docstatus": 1}
	if _has_field("Sales Invoice", "outstanding_amount"):
		filters["outstanding_amount"] = [">", 0]
	fields = _safe_fields("Sales Invoice", ["name", "grand_total", "outstanding_amount"])
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=fields,
		limit_page_length=0,
	)

	count = 0
	amount = 0
	for row in rows:
		payable = flt(_invoice_credit_payload(row).get("payable_amount"))
		if payable <= 0:
			continue
		count += 1
		amount += payable
	return {"count": count, "amount": amount}


def _doctype_available(doctype):
	try:
		return bool(frappe.db.exists("DocType", doctype)) and bool(frappe.db.table_exists(doctype))
	except Exception:
		return False


def _safe_fields(doctype, candidates):
	fields = []
	for fieldname in candidates:
		if fieldname == "name" or _has_field(doctype, fieldname):
			fields.append(fieldname)
	return fields or ["name"]


def _has_field(doctype, fieldname):
	try:
		if fieldname in {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}:
			return True
		if not _doctype_available(doctype):
			return False
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def _field_value(doc_or_row, fieldname):
	if hasattr(doc_or_row, "get"):
		return doc_or_row.get(fieldname)
	return getattr(doc_or_row, fieldname, None)


def _docdict(row):
	return dict(row) if isinstance(row, dict) else row.as_dict()


def _document_payload(doc):
	data = {}
	for field in doc.meta.fields:
		if field.fieldtype in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}:
			continue
		if field.fieldtype == "Table":
			data[field.fieldname] = [_child_payload(row) for row in (doc.get(field.fieldname) or [])]
		else:
			value = doc.get(field.fieldname)
			data[field.fieldname] = str(value) if hasattr(value, "isoformat") else value
	data["name"] = doc.name
	data["doctype"] = doc.doctype
	return data


def _child_payload(row):
	data = row.as_dict()
	for key, value in list(data.items()):
		if hasattr(value, "isoformat"):
			data[key] = str(value)
	return data


def _search_parents(query, limit):
	if not _doctype_available("Parent"):
		return []
	fields = _safe_fields("Parent", ["name", "parent_name", "mobile_number", "email", "customer"])
	return _search_doctype("Parent", query, fields, ["name", "parent_name", "mobile_number", "email"], limit)


def _search_students(query, limit):
	if not _doctype_available("Student"):
		return []
	fields = _safe_fields("Student", ["name", "student_name", "guardian", "parent", "date_of_birth", "status"])
	return _search_doctype("Student", query, fields, ["name", "student_name", "guardian", "parent"], limit)


def _search_customers(query, limit):
	if not _doctype_available("Customer"):
		return []
	fields = _safe_fields("Customer", ["name", "customer_name", "email_id", "mobile_no", "customer_type"])
	return _search_doctype("Customer", query, fields, ["name", "customer_name", "email_id", "mobile_no"], limit)


def _search_inquiries(query, limit):
	if not _doctype_available("Inquiry"):
		return []
	fields = [
		"name",
		"inquiry_type",
		"status",
		"campus",
		"parent",
		"student",
		"contact_name",
		"contact_phone",
		"contact_email",
		"current_appointment_date",
	]
	return _search_doctype("Inquiry", query, fields, ["name", "parent", "student", "contact_name", "contact_phone", "contact_email"], limit)


def _search_enrollments(query, limit):
	if not _doctype_available("Enrollment"):
		return []
	fields = _safe_fields(
		"Enrollment",
		["name", "student", "parent", "term", "course", "weekly_timeslot", "enrollment_type", "status", "invoice"],
	)
	return _search_doctype("Enrollment", query, fields, ["name", "student", "parent", "course", "weekly_timeslot", "invoice"], limit)


def _search_invoices(query, limit):
	if not _doctype_available("Sales Invoice"):
		return []
	invoice_names = _search_invoice_direct_names(query, limit)
	parent_ids, customer_ids, student_ids = _search_invoice_family_ids(query, limit)
	invoice_names.update(
		_search_invoice_names_for_family(
			parent_ids=parent_ids,
			customer_ids=customer_ids,
			student_ids=student_ids,
			limit=limit,
		)
	)
	return _get_invoice_rows(names=invoice_names, limit=limit)


def _search_invoice_direct_names(query, limit):
	fields = _safe_fields(
		"Sales Invoice",
		[
			"name",
			"customer",
			"parent",
			"student",
			"primary_student",
			"posting_date",
			"due_date",
			"status",
			"docstatus",
			"grand_total",
			"rounded_total",
			"outstanding_amount",
		],
	)
	rows = _search_doctype(
		"Sales Invoice",
		query,
		fields,
		["name", "customer", "parent", "student", "primary_student", "status"],
		limit,
	)
	return {row.get("name") for row in rows if row.get("name")}


def _search_invoice_family_ids(query, limit):
	parent_rows = _search_doctype(
		"Parent",
		query,
		_safe_fields("Parent", ["name", "customer"]),
		["name", "parent_name", "email", "email_id", "contact_email"],
		limit,
	)
	customer_rows = _search_doctype(
		"Customer",
		query,
		_safe_fields("Customer", ["name"]),
		["name", "customer_name", "email", "email_id", "contact_email"],
		limit,
	)
	student_rows = _search_doctype(
		"Student",
		query,
		_safe_fields("Student", ["name", "guardian", "parent"]),
		["name", "student_name"],
		limit,
	)

	parent_ids = {row.get("name") for row in parent_rows if row.get("name")}
	customer_ids = {row.get("customer") for row in parent_rows if row.get("customer")}
	customer_ids.update(row.get("name") for row in customer_rows if row.get("name"))
	student_ids = {row.get("name") for row in student_rows if row.get("name")}
	parent_field = _student_parent_field()
	if parent_field:
		parent_ids.update(row.get(parent_field) for row in student_rows if row.get(parent_field))

	if _doctype_available("Parent") and _has_field("Parent", "customer"):
		if parent_ids:
			rows = _get_invoice_search_parent_rows(
				filters={"name": ["in", sorted(parent_ids)]},
				fields=["name", "customer"],
			)
			customer_ids.update(row.customer for row in rows if row.customer)
		if customer_ids:
			rows = _get_invoice_search_parent_rows(
				filters={"customer": ["in", sorted(customer_ids)]},
				fields=["name"],
			)
			parent_ids.update(row.name for row in rows if row.name)

	return parent_ids, customer_ids, student_ids


def _get_invoice_search_parent_rows(filters, fields):
	try:
		return frappe.get_all("Parent", filters=filters, fields=fields, limit_page_length=0)
	except Exception:
		return []


def _search_invoice_names_for_family(parent_ids=None, customer_ids=None, student_ids=None, limit=50):
	invoice_names = set()
	or_filters = []
	if customer_ids:
		or_filters.append(["Sales Invoice", "customer", "in", sorted(customer_ids)])
	if parent_ids and _has_field("Sales Invoice", "parent"):
		or_filters.append(["Sales Invoice", "parent", "in", sorted(parent_ids)])
	if or_filters:
		rows = frappe.get_all(
			"Sales Invoice",
			or_filters=or_filters,
			fields=["name"],
			order_by="modified desc",
			limit=limit,
		)
		invoice_names.update(row.name for row in rows if row.name)
	if student_ids:
		invoice_names.update(_invoice_names_for_students(sorted(student_ids)))
	return invoice_names


def _search_doctype(doctype, query, fields, search_fields, limit):
	search_fields = [fieldname for fieldname in search_fields if fieldname == "name" or _has_field(doctype, fieldname)]
	if not search_fields:
		return []
	or_filters = [[doctype, fieldname, "like", f"%{query}%"] for fieldname in search_fields]
	try:
		rows = frappe.get_all(
			doctype,
			or_filters=or_filters,
			fields=fields,
			limit=limit,
			order_by="modified desc",
		)
	except Exception:
		return []
	return [_normalize_row_payload(doctype, row) for row in rows]


def _normalize_row_payload(doctype, row):
	data = _docdict(row)
	for key, value in list(data.items()):
		if hasattr(value, "isoformat"):
			data[key] = str(value)
	data["doctype"] = doctype
	return data


def _resolve_family_context(parent=None, student=None, customer=None, email=None):
	context = {"parent": parent, "student": student, "customer": customer}
	if student and not context.get("parent"):
		context["parent"] = _find_parent_for_student(student)
	if context.get("parent") and not context.get("customer") and _has_field("Parent", "customer"):
		context["customer"] = frappe.db.get_value("Parent", context["parent"], "customer")
	if context.get("customer") and not context.get("parent") and _has_field("Parent", "customer"):
		context["parent"] = frappe.db.get_value("Parent", {"customer": context["customer"]}, "name")
	if email and not context.get("parent"):
		context["parent"] = _find_parent_by_email(email)
	if email and not context.get("customer"):
		context["customer"] = _find_customer_by_email(email)
	if context.get("parent") and not context.get("customer") and _has_field("Parent", "customer"):
		context["customer"] = frappe.db.get_value("Parent", context["parent"], "customer")
	if student and not context.get("customer"):
		context["customer"] = _find_customer_for_student(student)
	return context


def _find_parent_for_student(student):
	if not student:
		return None
	parent_field = _student_parent_field()
	if parent_field:
		parent = frappe.db.get_value("Student", student, parent_field)
		if parent:
			return parent
	if _doctype_available("Enrollment") and _has_field("Enrollment", "parent"):
		rows = frappe.get_all(
			"Enrollment",
			filters={"student": student, "parent": ["is", "set"]},
			fields=["parent"],
			order_by="modified desc",
			limit=1,
		)
		parent = rows[0].parent if rows else None
		if parent:
			return parent
	if _doctype_available("Inquiry") and _has_field("Inquiry", "parent"):
		rows = frappe.get_all(
			"Inquiry",
			filters={"student": student, "parent": ["is", "set"]},
			fields=["parent"],
			order_by="modified desc",
			limit=1,
		)
		parent = rows[0].parent if rows else None
		if parent:
			return parent
	if _doctype_available("Sales Invoice") and _has_field("Sales Invoice", "parent"):
		for invoice in _invoice_names_for_students([student]):
			parent = frappe.db.get_value("Sales Invoice", invoice, "parent")
			if parent:
				return parent
	return None


def _find_customer_for_student(student):
	if not student or not _doctype_available("Sales Invoice"):
		return None
	for invoice in _invoice_names_for_students([student]):
		customer = frappe.db.get_value("Sales Invoice", invoice, "customer")
		if customer:
			return customer
	return None


def _student_parent_field():
	for fieldname in ["guardian", "parent"]:
		if _has_field("Student", fieldname):
			return fieldname
	return None


def _find_parent_by_email(email):
	for fieldname in ["email", "email_id", "contact_email"]:
		if _has_field("Parent", fieldname):
			parent = frappe.db.get_value("Parent", {fieldname: email}, "name")
			if parent:
				return parent
	return None


def _find_customer_by_email(email):
	for fieldname in ["email_id", "email", "contact_email"]:
		if _has_field("Customer", fieldname):
			customer = frappe.db.get_value("Customer", {fieldname: email}, "name")
			if customer:
				return customer
	return None


def _get_parent_payload(parent):
	fields = _safe_fields("Parent", ["name", *PARENT_EDIT_FIELDS, "linked_user", "modified"])
	rows = frappe.get_all("Parent", filters={"name": parent}, fields=fields, limit=1)
	if not rows:
		return {"doctype": "Parent", "name": parent}
	return _attach_parent_portal_invite_statuses([_normalize_row_payload("Parent", rows[0])])[0]


def _attach_parent_portal_invite_statuses(parents):
	if not parents:
		return []
	try:
		from qas_custom.services.portal_invites import get_parent_portal_invite_status
	except Exception:
		return parents
	for row in parents:
		try:
			row["portal_invite_status"] = get_parent_portal_invite_status(row)
		except Exception:
			row["portal_invite_status"] = {"status": "unknown", "label": _("Unknown"), "bulk_eligible": False}
	return parents


def _get_customer_payload(customer):
	fields = _safe_fields("Customer", ["name", "customer_name", "email_id", "mobile_no", "customer_type", "customer_group"])
	rows = frappe.get_all("Customer", filters={"name": customer}, fields=fields, limit=1)
	return _normalize_row_payload("Customer", rows[0]) if rows else {"doctype": "Customer", "name": customer}


def _get_family_students(parent=None, student=None):
	parent_field = _student_parent_field()
	if parent and parent_field:
		filters = {parent_field: parent}
	elif student:
		filters = {"name": student}
	else:
		return []
	fields = _safe_fields("Student", ["name", *STUDENT_EDIT_FIELDS, "age", "student_code", "modified"])
	rows = frappe.get_all("Student", filters=filters, fields=fields, order_by="student_name asc")
	return [_normalize_row_payload("Student", row) for row in rows]


def _get_family_inquiry_rows(parent=None, students=None, email=None, limit=80):
	if not _doctype_available("Inquiry"):
		return []
	or_filters = []
	if parent:
		or_filters.append(["Inquiry", "parent", "=", parent])
	for student in students or []:
		or_filters.append(["Inquiry", "student", "=", student])
	if email:
		or_filters.append(["Inquiry", "contact_email", "=", email])
	if not or_filters:
		return []
	fields = _safe_fields(
		"Inquiry",
		[
			"name",
			"inquiry_type",
			"status",
			"campus",
			"parent",
			"student",
			"contact_name",
			"contact_phone",
			"contact_email",
			"preferred_course",
			"current_appointment_date",
			"current_appointment_time",
			"converted_enrollment",
			"converted_invoice",
		],
	)
	rows = frappe.get_all(
		"Inquiry",
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return [_build_inquiry_list_item(row) for row in rows]


def _build_inquiry_list_item(row):
	payload = build_inquiry_summary(row)
	payload["latest_note"] = _get_latest_inquiry_note(row.name)
	return payload


def _get_latest_inquiry_note(inquiry):
	rows = frappe.get_all(
		"Inquiry Note",
		filters={"inquiry": inquiry},
		fields=["note", "creation"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0].note if rows else None


def _inquiry_order_by(queue):
	if not queue or queue == "post_visit":
		return "current_appointment_date desc, modified desc, name desc"
	return "current_appointment_date asc, current_appointment_time asc, modified desc, name asc"


def _get_invoice_rows(status=None, customer=None, parent=None, students=None, source=None, names=None, limit=80):
	if not _doctype_available("Sales Invoice"):
		return []
	filters = {}
	if names is not None:
		names = sorted({name for name in names if name})
		if not names:
			return []
		filters["name"] = ["in", names]
	if status:
		_apply_invoice_status_filter(filters, status)
	if customer:
		filters["customer"] = customer
	if parent and _has_field("Sales Invoice", "parent"):
		filters["parent"] = parent
	if students:
		invoice_names = _invoice_names_for_students(students)
		if not invoice_names:
			return []
		if names is not None:
			invoice_names = set(names).intersection(invoice_names)
			if not invoice_names:
				return []
		filters["name"] = ["in", sorted(invoice_names)]
	if source:
		_apply_invoice_source_filter(filters, source)
	fields = _safe_fields(
		"Sales Invoice",
		[
			"name",
			"customer",
			"posting_date",
			"due_date",
			"status",
			"docstatus",
			"grand_total",
			"rounded_total",
			"outstanding_amount",
			"parent",
			"student",
			"primary_student",
			"student_summary",
			"enrollment",
			"course",
			"term",
			"qas_invoice_type",
			"source_doctype",
			"source_document",
			"billing_note",
			"source_inquiry",
		],
	)
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	from qas_custom.services.payment_collection_requests import get_invoice_payment_request_summaries

	payment_request_summaries = get_invoice_payment_request_summaries(row.get("name") for row in rows)
	return [_invoice_row_payload(row, payment_request_summaries.get(row.get("name"))) for row in rows]


def _invoice_row_payload(row, payment_request_summary=None):
	from qas_custom.services.payment_collection_requests import get_invoice_payment_request_summary

	payload = _normalize_row_payload("Sales Invoice", row)
	payload.update(_invoice_credit_payload(payload))
	payload.update(payment_request_summary or get_invoice_payment_request_summary(payload.get("name")))
	return payload


def _apply_invoice_status_filter(filters, status):
	status = status.strip()
	if status == "Draft":
		filters["docstatus"] = 0
	elif status == "Submitted":
		filters["docstatus"] = 1
	elif status == "Cancelled":
		filters["docstatus"] = 2
	elif _has_field("Sales Invoice", "status"):
		filters["status"] = status


def _apply_invoice_source_filter(filters, source):
	if source == "Inquiry" and _has_field("Sales Invoice", "source_inquiry"):
		filters["source_inquiry"] = ["is", "set"]
	elif source == "Enrollment" and _has_field("Sales Invoice", "enrollment"):
		filters["enrollment"] = ["is", "set"]
	elif source == "Manual":
		if _has_field("Sales Invoice", "qas_is_manual_invoice"):
			filters["qas_is_manual_invoice"] = 1
		elif _has_field("Sales Invoice", "source_type"):
			filters["source_type"] = "Manual"


def _invoice_names_for_students(students):
	names = set()
	if _has_field("Sales Invoice", "student"):
		names.update(
			frappe.get_all(
				"Sales Invoice",
				filters={"student": ["in", students]},
				pluck="name",
				limit_page_length=0,
			)
		)
	if _has_field("Sales Invoice", "primary_student"):
		names.update(
			frappe.get_all(
				"Sales Invoice",
				filters={"primary_student": ["in", students]},
				pluck="name",
				limit_page_length=0,
			)
		)
	if _doctype_available("Sales Invoice Item") and _has_field("Sales Invoice Item", "student"):
		names.update(
			frappe.get_all(
				"Sales Invoice Item",
				filters={"student": ["in", students]},
				pluck="parent",
				limit_page_length=0,
			)
		)
	return names


def _build_invoice_payload(doc):
	from qas_custom.services.payment_collection_requests import get_invoice_payment_request_summary

	doc = frappe.get_doc("Sales Invoice", doc) if isinstance(doc, str) else doc
	payload = _document_payload(doc)
	payload["docstatus"] = cint(doc.docstatus)
	payload["status_label"] = _invoice_status_label(doc)
	payload["items"] = [_child_payload(row) for row in doc.get("items", [])]
	payload["comments"] = _get_comments("Sales Invoice", doc.name)
	payload.update(_invoice_credit_payload(doc))
	payload["notifications"] = get_invoice_notification_summary(doc.name)
	payload.update(get_invoice_payment_request_summary(doc.name))
	return payload


def _apply_invoice_payment_payload(doc, payload):
	for fieldname in SNAPSHOT_FIELD_MAP.values():
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))


def _invoice_credit_payload(doc_or_row):
	invoice_name = _field_value(doc_or_row, "name")
	store_credit_applied = get_invoice_store_credit_applied(invoice_name) if invoice_name else 0
	payable_amount = get_invoice_payable_amount(doc_or_row) if invoice_name else 0
	return {
		"store_credit_applied": store_credit_applied,
		"payable_amount": payable_amount,
		"invoice_total": get_invoice_total_amount(doc_or_row),
		"invoice_link": _invoice_link(invoice_name) if invoice_name else None,
		"payment_link": _invoice_payment_link(invoice_name) if invoice_name else None,
	}


def _invoice_link(invoice):
	return parent_portal_invoice_link(invoice)


def _invoice_payment_link(invoice):
	return parent_portal_invoice_link(invoice)


def _invoice_status_label(doc):
	if cint(doc.docstatus) == 0:
		return doc.get("status") or "Draft"
	if cint(doc.docstatus) == 1:
		return doc.get("status") or "Submitted"
	if cint(doc.docstatus) == 2:
		return "Cancelled"
	return doc.get("status")


def _apply_invoice_items(invoice, items):
	if not items:
		frappe.throw(_("At least one invoice item is required."))
	invoice.set("items", [])
	for row in items:
		item_code = row.get("item_code") or row.get("item")
		if not item_code:
			frappe.throw(_("Invoice item code is required."))
		if item_code == MANUAL_INVOICE_ITEM:
			item_code = _ensure_school_admin_invoice_item(MANUAL_INVOICE_ITEM)
		item = invoice.append(
			"items",
			{
				"item_code": item_code,
				"item_name": row.get("item_name") or item_code,
				"description": row.get("description") or row.get("item_name") or item_code,
				"qty": flt(row.get("qty") or 1),
				"rate": flt(row.get("rate") or 0),
			},
		)
		student = row.get("student")
		_set_if_field(item, "qas_line_type", row.get("qas_line_type") or row.get("line_type") or "Other")
		_set_if_field(item, "student", student)
		_set_if_field(item, "student_display_name", row.get("student_display_name") or (get_student_parent_name(student) if student else None))
		_set_if_field(item, "student_code", row.get("student_code") or (get_student_display_code(student) if student else None))
		_set_if_field(item, "enrollment", row.get("enrollment"))
		_set_if_field(item, "course", row.get("course"))
		_set_if_field(item, "term", row.get("term"))
		_set_if_field(item, "course_session", row.get("course_session"))
		_set_if_field(item, "session_count", row.get("session_count"))


def _sync_invoice_student_summary(invoice):
	students = []
	seen = set()
	for item in invoice.get("items", []):
		student = item.get("student") if hasattr(item, "get") else None
		if student and student not in seen:
			seen.add(student)
			students.append(student)
	if not students:
		return

	_set_if_field(invoice, "primary_student", students[0])
	labels = [get_student_parent_name(student) or student for student in students]
	summary = labels[0] if len(labels) == 1 else _("Multiple students: {0}").format(", ".join(labels))
	_set_if_field(invoice, "student_summary", summary)


def _run_school_admin_invoice_mutation(callback):
	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		return callback()
	finally:
		frappe.set_user(original_user)


def _cancel_submitted_invoice_as_admin(invoice):
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))

	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Sales Invoice", invoice)
		doc.flags.ignore_permissions = True
		doc.cancel()
	finally:
		frappe.set_user(original_user)


def _cancel_invoice_payment_entries(invoice):
	if not _doctype_available("Payment Entry Reference") or not _doctype_available("Payment Entry"):
		return []
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))
	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
			"parenttype": "Payment Entry",
		},
		fields=["parent"],
		limit_page_length=0,
	)
	payment_entries = sorted({row.get("parent") for row in rows if row.get("parent")})
	cancelled = []
	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		for payment_entry_name in payment_entries:
			payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)
			if cint(payment_entry.docstatus) != 1:
				continue
			payment_entry.flags.ignore_permissions = True
			payment_entry.cancel()
			cancelled.append(payment_entry.name)
	finally:
		frappe.set_user(original_user)
	return cancelled


def _invoice_payment_amount(invoice):
	if not _doctype_available("Payment Entry Reference") or not _doctype_available("Payment Entry"):
		return 0
	rows = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
			"parenttype": "Payment Entry",
		},
		fields=["parent", "allocated_amount"],
		limit_page_length=0,
	)
	if not rows:
		return 0
	payment_names = sorted({row.get("parent") for row in rows if row.get("parent")})
	if not payment_names:
		return 0
	submitted_payments = set(
		frappe.get_all(
			"Payment Entry",
			filters={"name": ["in", payment_names], "docstatus": 1},
			pluck="name",
			limit_page_length=0,
		)
	)
	return flt(sum(flt(row.get("allocated_amount")) for row in rows if row.get("parent") in submitted_payments))


def _create_invoice_cancellation_store_credit(doc, amount, reason):
	amount = flt(amount)
	if amount <= 0 or not doc.get("customer"):
		return None
	notes = _("Moved paid amount to store credit because invoice {0} was cancelled.").format(doc.name)
	if reason:
		notes = _("{0} Reason: {1}").format(notes, reason)
	credit = create_store_credit_entry(
		parent=doc.get("parent"),
		customer=doc.get("customer"),
		student=doc.get("primary_student") or doc.get("student"),
		transaction_type="Correction",
		credit_amount=amount,
		payment_amount=amount,
		invoice=doc.name,
		enrollment=doc.get("enrollment"),
		reference_doctype="Sales Invoice",
		reference_document=doc.name,
		source_doctype="Sales Invoice",
		source_document=doc.name,
		reason="Paid invoice cancellation",
		notes=notes,
	)
	_add_comment("Sales Invoice", doc.name, _("Paid amount moved to store credit: {0}.").format(amount))
	return credit


def _detach_invoice_operation_report_links(invoice):
	if not invoice or not _doctype_available("QAS Operation Report Row"):
		return []
	rows = frappe.get_all(
		"QAS Operation Report Row",
		filters={"invoice": invoice},
		fields=["name", "parent", "message", "reference_doctype", "reference_name", "raw_row_json"],
		limit_page_length=0,
	)
	detached = []
	for row in rows:
		message = str(row.get("message") or "").strip()
		reference_text = " ".join(
			str(row.get(fieldname) or "")
			for fieldname in ("reference_doctype", "reference_name", "message", "raw_row_json")
		)
		updates = {"invoice": None}
		if invoice not in reference_text:
			audit_note = _("Draft invoice {0} was deleted; its original reference is retained in this operation report.").format(invoice)
			updates["message"] = f"{message}\n{audit_note}".strip()
		frappe.db.set_value(
			"QAS Operation Report Row",
			row.get("name"),
			updates,
			update_modified=False,
		)
		detached.append({"row": row.get("name"), "report": row.get("parent")})
	return detached


def _clear_deleted_invoice_enrollment_snapshot(doc, action="deleted"):
	enrollment_names = set()
	for candidate in [doc.get("enrollment")]:
		if candidate:
			enrollment_names.add(candidate)
	if doc.get("source_doctype") == "Enrollment" and doc.get("source_document"):
		enrollment_names.add(doc.get("source_document"))
	for item in doc.get("items", []):
		if item.get("enrollment"):
			enrollment_names.add(item.get("enrollment"))

	for enrollment in sorted(enrollment_names):
		if not frappe.db.exists("Enrollment", enrollment):
			continue
		current_invoice = frappe.db.get_value("Enrollment", enrollment, "invoice") if _has_field("Enrollment", "invoice") else None
		if current_invoice and current_invoice != doc.name:
			continue
		updates = {}
		for fieldname, value in {"invoice": None, "invoice_status": None, "invoice_amount": 0}.items():
			if _has_field("Enrollment", fieldname):
				updates[fieldname] = value
		if updates:
			frappe.db.set_value("Enrollment", enrollment, updates, update_modified=True)
			if action == "cancelled":
				message = _("Invoice {0} was cancelled by School Admin; enrollment invoice link was cleared.")
			else:
				message = _("Draft invoice {0} was deleted by School Admin.")
			_add_comment("Enrollment", enrollment, message.format(doc.name))


def _reopen_unpaid_invoice_safety_check(doc):
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted invoices can be reopened for correction."))
	paid_amount = _invoice_payment_amount(doc.name)
	if paid_amount > 0.005:
		frappe.throw(_("This invoice has payment entries applied and cannot be reopened. Cancel and reissue it instead."))
	store_credit_applied = get_invoice_store_credit_applied(doc.name)
	if store_credit_applied > 0.005 or has_invoice_store_credit_journal_entry(doc.name):
		frappe.throw(_("This invoice has store credit applied and cannot be reopened. Cancel and reissue it instead."))


def _invoice_amendment_for(invoice):
	if not invoice or not _has_field("Sales Invoice", "amended_from"):
		return None
	return frappe.db.get_value(
		"Sales Invoice",
		{"amended_from": invoice},
		"name",
		order_by="creation desc",
	)


def _move_enrollment_invoice_snapshots_to_amendment(original, amendment, reason):
	enrollment_names = set()
	for candidate in [original.get("enrollment")]:
		if candidate:
			enrollment_names.add(candidate)
	if original.get("source_doctype") == "Enrollment" and original.get("source_document"):
		enrollment_names.add(original.get("source_document"))
	for item in original.get("items", []):
		if item.get("enrollment"):
			enrollment_names.add(item.get("enrollment"))

	for enrollment in sorted(enrollment_names):
		if not frappe.db.exists("Enrollment", enrollment):
			continue
		current_invoice = frappe.db.get_value("Enrollment", enrollment, "invoice") if _has_field("Enrollment", "invoice") else None
		if current_invoice and current_invoice != original.name:
			continue
		updates = {}
		for fieldname, value in {
			"invoice": amendment.name,
			"invoice_status": "Draft",
			"invoice_amount": amendment.get("grand_total"),
		}.items():
			if _has_field("Enrollment", fieldname):
				updates[fieldname] = value
		if updates:
			frappe.db.set_value("Enrollment", enrollment, updates, update_modified=True)
			_add_comment(
				"Enrollment",
				enrollment,
				_("Invoice {0} was cancelled for correction and replaced with draft {1}. Reason: {2}").format(
					original.name, amendment.name, reason
				),
			)


def _reverse_invoice_store_credit_application(doc, reason):
	applied = get_invoice_store_credit_applied(doc.name)
	if applied <= 0:
		return None
	parent = doc.get("parent")
	customer = doc.get("customer")
	if not customer:
		return None
	notes = _("Reversed store credit because invoice {0} was cancelled.").format(doc.name)
	if reason:
		notes = _("{0} Reason: {1}").format(notes, reason)
	return create_store_credit_entry(
		parent=parent,
		customer=customer,
		student=doc.get("primary_student") or doc.get("student"),
		transaction_type="Correction",
		credit_amount=applied,
		invoice=doc.name,
		enrollment=doc.get("enrollment"),
		reference_doctype="Sales Invoice",
		reference_document=doc.name,
		source_doctype="Sales Invoice",
		source_document=doc.name,
		reason="Invoice cancellation",
		notes=notes,
	)


def _send_invoice_notification(doc, event="approved", store_credit_applied=None):
	return send_parent_invoice_notification(
		doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=None,
	)


def _enqueue_invoice_notification(doc, event="approved", store_credit_applied=None):
	return enqueue_parent_invoice_notification(
		doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=None,
	)


def _maybe_send_paid_receipt(doc, *, payment_entry=None, source=None):
	return maybe_send_parent_invoice_paid_receipt(
		doc,
		payment_entry=payment_entry,
		source=source,
	)


def _enqueue_paid_receipt(doc, *, payment_entry=None, source=None):
	return enqueue_parent_invoice_paid_receipt(
		doc,
		payment_entry=payment_entry,
		source=source,
	)


def _skipped_invoice_notification(reason, receipt=False):
	return {"sent": False, "skipped": True, "reason": reason, "receipt": receipt}


def _create_payment_entry_for_invoice(doc, amount, mode_of_payment=None, reference_no=None, notes=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))

	original_user = frappe.session.user or "Administrator"
	try:
		frappe.set_user("Administrator")
		payment_entry = get_payment_entry("Sales Invoice", doc.name)
		payment_entry.flags.ignore_permissions = True
		amount = flt(amount)
		if mode_of_payment and payment_entry.meta.has_field("mode_of_payment"):
			payment_entry.mode_of_payment = mode_of_payment
		if reference_no and payment_entry.meta.has_field("reference_no"):
			payment_entry.reference_no = reference_no
		elif payment_entry.meta.has_field("reference_no"):
			payment_entry.reference_no = _("School Admin payment {0}").format(now_datetime())
		if payment_entry.meta.has_field("reference_date"):
			payment_entry.reference_date = nowdate()
		if payment_entry.meta.has_field("remarks") and notes:
			payment_entry.remarks = notes
		if payment_entry.meta.has_field("paid_amount"):
			payment_entry.paid_amount = amount
		if payment_entry.meta.has_field("received_amount"):
			payment_entry.received_amount = amount

		remaining = amount
		for reference in payment_entry.get("references", []):
			if reference.reference_doctype != "Sales Invoice" or reference.reference_name != doc.name:
				continue
			allocatable = flt(reference.outstanding_amount) or remaining
			reference.allocated_amount = min(remaining, allocatable)
			remaining -= flt(reference.allocated_amount)
			if remaining <= 0:
				break

		payment_entry.insert(ignore_permissions=True)
		payment_entry.submit()
		return payment_entry
	finally:
		frappe.set_user(original_user)


def _get_enrollment_rows(parent=None, students=None, filters=None, limit=80):
	if not _doctype_available("Enrollment"):
		return []
	filters = dict(filters or {})
	if parent:
		filters["parent"] = parent
	if students:
		filters["student"] = ["in", students]
	fields = _safe_fields(
		"Enrollment",
		[
			"name",
			"student",
			"parent",
			"term",
			"course",
			"weekly_timeslot",
			"start_course_session",
			"enrollment_type",
			"status",
			"enrollment_date",
			"invoice",
			"invoice_status",
			"invoice_amount",
			"remaining_sessions",
			"source_inquiry",
		],
	)
	rows = frappe.get_all(
		"Enrollment",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=limit,
	)
	return _attach_course_labels([_normalize_row_payload("Enrollment", row) for row in rows])


def _get_attendance_candidate_enrollment_names(parent=None, students=None, term=None):
	if not _doctype_available("Enrollment"):
		return []
	filters = {
		"status": ["in", ["Planned", "Active"]],
		"enrollment_type": "Full-Term",
	}
	if parent:
		filters["parent"] = parent
	elif students:
		filters["student"] = ["in", students]
	if term:
		filters["term"] = term
	if not parent and not students and not term:
		return []
	return frappe.get_all(
		"Enrollment",
		filters=filters,
		pluck="name",
		order_by="weekly_timeslot asc, student asc",
		limit_page_length=0,
	)


def _get_invoice_candidate_enrollment_names(parent=None, students=None, term=None):
	if not _doctype_available("Enrollment"):
		return []
	filters = {
		"status": "Active",
		"enrollment_type": "Full-Term",
	}
	if parent:
		filters["parent"] = parent
	elif students:
		filters["student"] = ["in", students]
	if term:
		filters["term"] = term
	if not parent and not students and not term:
		return []
	return frappe.get_all(
		"Enrollment",
		filters=filters,
		pluck="name",
		order_by="parent asc, term asc, weekly_timeslot asc, student asc",
		limit_page_length=0,
	)


def _apply_enrollment_payload(doc, payload):
	for fieldname in [
		"student",
		"parent",
		"term",
		"course",
		"weekly_timeslot",
		"start_course_session",
		"enrollment_type",
		"status",
		"trial_session_date",
		"enrollment_date",
		"invoice",
		"invoice_status",
		"invoice_amount",
		"remaining_sessions",
		"source_inquiry",
	]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if not doc.get("parent") and doc.get("student") and _has_field("Student", "guardian"):
		_set_if_field(doc, "parent", frappe.db.get_value("Student", doc.student, "guardian"))
	if not doc.get("status"):
		_set_if_field(doc, "status", "Active")
	if not doc.get("enrollment_type"):
		_set_if_field(doc, "enrollment_type", "Full-Term")
	if not doc.get("enrollment_date"):
		_set_if_field(doc, "enrollment_date", today())


def _create_enrollment_attendance_entries(doc, start_date=None):
	if doc.get("status") != "Active":
		return []
	if doc.get("enrollment_type") != "Full-Term" or not doc.get("weekly_timeslot") or not doc.get("student"):
		return []
	filters = {"weekly_timeslot": doc.weekly_timeslot, "status": ["!=", "Cancelled"]}
	if start_date:
		filters["session_date"] = [">=", getdate(start_date)]
	elif doc.get("start_course_session"):
		start_session_date = frappe.db.get_value("Course Sessions", doc.start_course_session, "session_date")
		if start_session_date:
			filters["session_date"] = [">=", getdate(start_session_date)]
	rows = frappe.get_all("Course Sessions", filters=filters, fields=["name", "session_date"], order_by="session_date asc")
	create_full_term_attendance_entries(rows, doc.student, doc.name)
	return [row.name for row in rows]


def _create_attendance_for_enrollment_names(enrollment_names, payload=None):
	payload = payload or {}
	summary = {
		"eligible": len(enrollment_names or []),
		"activated_enrollments": 0,
		"attendance_entries": 0,
		"skipped": 0,
		"errors": 0,
		"error_rows": [],
	}
	for enrollment in enrollment_names or []:
		savepoint = f"attendance_enrollment_{enrollment}".replace("-", "_")
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.get_doc("Enrollment", enrollment)
			if doc.get("status") == "Planned":
				term_doc = frappe.get_doc("Term", doc.term)
				result = _activate_planned_enrollment(
					doc,
					term_doc,
					start_session=payload.get("start_course_session") or doc.get("start_course_session"),
				)
				summary["activated_enrollments"] += 1
				summary["attendance_entries"] += cint(result.get("attendance_entries") or 0)
				continue
			if doc.get("status") != "Active" or doc.get("enrollment_type") != "Full-Term":
				summary["skipped"] += 1
				continue
			term_doc = frappe.get_doc("Term", doc.term)
			start_session = _validate_enrollment_start_session(
				payload.get("start_course_session") or doc.get("start_course_session") or _first_course_session_for_timeslot(doc.weekly_timeslot, term_doc),
				doc.weekly_timeslot,
				term_doc,
			)
			_set_if_field(doc, "start_course_session", start_session)
			if not doc.get("enrollment_date"):
				start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
				_set_if_field(doc, "enrollment_date", start_date or term_doc.get("start_date") or today())
			doc.save(ignore_permissions=True)
			attendance_entries = _create_enrollment_attendance_entries(doc)
			_add_comment("Enrollment", doc.name, _("Attendance prepared by School Admin."))
			summary["attendance_entries"] += len(attendance_entries)
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			summary["errors"] += 1
			summary["error_rows"].append({"enrollment": enrollment, "error": _bulk_action_error_message(exc)})
	return summary


def _next_invoice_candidate_batch(enrollment_names, batch_size):
	batch_size = max(1, cint(batch_size))
	pending = []
	remaining_before = 0
	for enrollment in enrollment_names or []:
		doc = frappe.get_doc("Enrollment", enrollment)
		if _existing_invoice_for_enrollment(doc):
			continue
		remaining_before += 1
		if len(pending) < batch_size:
			pending.append(enrollment)
	remaining_after = max(remaining_before - len(pending), 0)
	return pending, {
		"batch_size": batch_size,
		"remaining_before": remaining_before,
		"remaining_after": remaining_after,
		"has_more": remaining_after > 0,
	}


def _create_invoices_for_enrollment_names(enrollment_names, payload=None):
	payload = payload or {}
	summary = {
		"eligible": len(enrollment_names or []),
		"created_invoices": 0,
		"invoice_items": 0,
		"skipped": 0,
		"errors": 0,
		"invoices": [],
		"warnings": [],
		"error_rows": [],
	}
	created_invoice_names = set()
	invoice_names = set()
	for enrollment in enrollment_names or []:
		savepoint = f"invoice_enrollment_{enrollment}".replace("-", "_")
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.get_doc("Enrollment", enrollment)
			if doc.get("status") != "Active" or doc.get("enrollment_type") != "Full-Term":
				summary["skipped"] += 1
				continue
			existing_invoice = _existing_invoice_for_enrollment(doc)
			if existing_invoice:
				summary["skipped"] += 1
				continue
			if not _enrollment_has_attendance(doc.name):
				summary["warnings"].append({
					"enrollment": doc.name,
					"warning": _("No attendance rows found for this enrollment."),
				})
			term_doc = frappe.get_doc("Term", doc.term)
			start_session = _validate_enrollment_start_session(
				payload.get("start_course_session") or doc.get("start_course_session") or _first_course_session_for_timeslot(doc.weekly_timeslot, term_doc),
				doc.weekly_timeslot,
				term_doc,
			)
			timeslot_course = frappe.db.get_value("Weekly Timeslot", doc.weekly_timeslot, "course")
			_set_if_field(doc, "course", doc.get("course") or timeslot_course)
			if not doc.get("enrollment_date"):
				start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
				_set_if_field(doc, "enrollment_date", start_date or term_doc.get("start_date") or today())
			invoice = _create_term_enrollment_invoice(doc, start_session)
			_set_if_field(doc, "start_course_session", start_session)
			_set_if_field(doc, "invoice", invoice.name)
			_set_if_field(doc, "invoice_status", "Draft")
			_set_if_field(doc, "invoice_amount", invoice.get("grand_total"))
			doc.save(ignore_permissions=True)
			_add_comment("Enrollment", doc.name, _("Draft invoice {0} created by School Admin.").format(invoice.name))
			invoice_names.add(invoice.name)
			if getattr(invoice.flags, "qas_was_created", False):
				created_invoice_names.add(invoice.name)
			summary["invoice_items"] += 1
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			summary["errors"] += 1
			summary["error_rows"].append({"enrollment": enrollment, "error": _bulk_action_error_message(exc)})
	summary["created_invoices"] = len(created_invoice_names)
	summary["invoices"] = sorted(invoice_names)
	return summary


def _enrollment_has_attendance(enrollment):
	if not enrollment or not _doctype_available("Class Attendance Entry"):
		return False
	return bool(frappe.db.exists(
		"Class Attendance Entry",
		{
			"source_doctype": "Enrollment",
			"source_document": enrollment,
			"status": ["!=", "Cancelled"],
		},
	))


def _build_enrollment_transfer_preview(doc, target_timeslot, effective_date):
	if doc.get("status") != "Active":
		frappe.throw(_("Only active enrollments can be transferred."))
	if doc.get("enrollment_type") != "Full-Term":
		frappe.throw(_("Only Full-Term enrollments can be transferred."))
	if not doc.get("weekly_timeslot"):
		frappe.throw(_("The enrollment does not have a current weekly timeslot."))
	if doc.get("weekly_timeslot") == target_timeslot:
		frappe.throw(_("Choose a different weekly timeslot for the transfer."))
	if not frappe.db.exists("Weekly Timeslot", target_timeslot):
		frappe.throw(_("Target weekly timeslot was not found."))

	target = frappe.db.get_value(
		"Weekly Timeslot",
		target_timeslot,
		["name", "term", "course", "status"],
		as_dict=True,
	)
	if not target or target.get("term") != doc.get("term"):
		frappe.throw(_("The destination class must belong to the same term as the enrollment."))
	if target.get("status") and target.get("status") != "Active":
		frappe.throw(_("The destination weekly timeslot must be active."))
	duplicate = _existing_target_enrollment(
		doc.get("student"),
		doc.get("term"),
		target_timeslot,
		statuses=["Planned", "Active"],
	)
	if duplicate and duplicate != doc.name:
		frappe.throw(_("This student already has an open enrollment in the destination class: {0}.").format(duplicate))

	source_sessions = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": doc.get("weekly_timeslot"),
			"session_date": [">=", effective_date],
		},
		fields=["name", "session_date", "status"],
		order_by="session_date asc, name asc",
		limit_page_length=0,
	)
	target_sessions = frappe.get_all(
		"Course Sessions",
		filters={
			"weekly_timeslot": target_timeslot,
			"session_date": [">=", effective_date],
			"status": ["!=", "Cancelled"],
		},
		fields=["name", "session_date", "status"],
		order_by="session_date asc, name asc",
		limit_page_length=0,
	)
	if not target_sessions:
		frappe.throw(_("The destination class has no sessions on or after the effective date."))

	source_session_ids = [row.get("name") for row in source_sessions if row.get("name")]
	attendance_rows = []
	if source_session_ids:
		attendance_rows = frappe.get_all(
			"Class Attendance Entry",
			filters={
				"source_doctype": "Enrollment",
				"source_document": doc.name,
				"course_session": ["in", source_session_ids],
			},
			fields=["name", "course_session", "status"],
			order_by="course_session asc, creation asc",
			limit_page_length=0,
		)
	session_date_by_id = {row.get("name"): row.get("session_date") for row in source_sessions}
	cancellable_rows = [
		row for row in attendance_rows
		if row.get("status") in TRANSFER_CANCELLABLE_ATTENDANCE_STATUSES
	]
	retained_marked_rows = [
		{
			"name": row.get("name"),
			"course_session": row.get("course_session"),
			"session_date": str(session_date_by_id.get(row.get("course_session")) or ""),
			"status": row.get("status"),
		}
		for row in attendance_rows
		if row.get("status") in TRANSFER_RETAINED_ATTENDANCE_STATUSES
	]
	retained_week_keys = {
		getdate(row.get("session_date")).isocalendar()[:2]
		for row in retained_marked_rows
		if row.get("session_date")
	}
	eligible_target_sessions = [
		row for row in target_sessions
		if getdate(row.get("session_date")).isocalendar()[:2] not in retained_week_keys
	]
	preview_fingerprint = hashlib.sha256(json.dumps({
		"enrollment": doc.name,
		"source_timeslot": doc.get("weekly_timeslot"),
		"target_timeslot": target_timeslot,
		"effective_date": str(effective_date),
		"source_attendance": sorted(
			(row.get("name"), row.get("course_session"), row.get("status"))
			for row in attendance_rows
		),
		"target_sessions": [row.get("name") for row in eligible_target_sessions if row.get("name")],
	}, sort_keys=True).encode("utf-8")).hexdigest()
	return {
		"source_timeslot": doc.get("weekly_timeslot"),
		"target_timeslot": target_timeslot,
		"target_course": target.get("course"),
		"target_term": target.get("term"),
		"effective_date": str(effective_date),
		"source_session_ids": source_session_ids,
		"target_session_ids": [row.get("name") for row in eligible_target_sessions if row.get("name")],
		"target_start_course_session": (
			eligible_target_sessions[0].get("name") if eligible_target_sessions else target_sessions[0].get("name")
		),
		"cancellable_count": len(cancellable_rows),
		"destination_session_count": len(eligible_target_sessions),
		"destination_sessions_skipped_for_marked_count": len(target_sessions) - len(eligible_target_sessions),
		"retained_marked_count": len(retained_marked_rows),
		"retained_marked_rows": retained_marked_rows,
		"financial_records_changed": False,
		"preview_fingerprint": preview_fingerprint,
	}


def _cancel_enrollment_attendance_for_sessions(enrollment, session_ids, statuses=None):
	if not enrollment or not session_ids or not _doctype_available("Class Attendance Entry"):
		return 0
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters={
			"source_doctype": "Enrollment",
			"source_document": enrollment,
			"course_session": ["in", session_ids],
			"status": ["in", sorted(statuses or TRANSFER_CANCELLABLE_ATTENDANCE_STATUSES)],
		},
		pluck="name",
		limit_page_length=0,
	)
	for row in rows:
		frappe.db.set_value("Class Attendance Entry", row, "status", "Cancelled", update_modified=True)
	return len(rows)


def _ensure_transfer_destination_attendance(doc, session_ids):
	result = {"created": 0, "reactivated": 0, "retained": 0, "total": 0}
	for session_id in session_ids or []:
		existing = frappe.db.get_value(
			"Class Attendance Entry",
			{
				"source_doctype": "Enrollment",
				"source_document": doc.name,
				"course_session": session_id,
			},
			["name", "status"],
			as_dict=True,
		)
		if existing:
			if existing.get("status") == "Cancelled":
				frappe.db.set_value(
					"Class Attendance Entry",
					existing.get("name"),
					"status",
					"To be started",
					update_modified=True,
				)
				result["reactivated"] += 1
			else:
				result["retained"] += 1
			result["total"] += 1
			continue
		create_attendance_entry(
			course_session=session_id,
			student=doc.student,
			enrollment_type="Full-Term",
			source_doctype="Enrollment",
			source_document=doc.name,
			status="To be started",
			comments=f"Added from Enrollment {doc.name} after class transfer",
		)
		result["created"] += 1
		result["total"] += 1
	return result


def _cancel_future_enrollment_attendance(enrollment, effective_date=None):
	if not _doctype_available("Class Attendance Entry"):
		return 0
	session_ids = []
	if effective_date:
		session_ids = frappe.get_all(
			"Course Sessions",
			filters={"session_date": [">=", getdate(effective_date)]},
			pluck="name",
			limit_page_length=0,
		)
		if not session_ids:
			return 0
	filters = {
		"source_doctype": "Enrollment",
		"source_document": enrollment,
		"status": ["in", ["To be started", "Scheduled"]],
	}
	if session_ids:
		filters["course_session"] = ["in", session_ids]
	rows = frappe.get_all("Class Attendance Entry", filters=filters, pluck="name", limit_page_length=0)
	for row in rows:
		frappe.db.set_value("Class Attendance Entry", row, "status", "Cancelled", update_modified=True)
	return len(rows)


def _build_enrollment_payload(doc):
	payload = _document_payload(doc)
	_attach_course_label(payload, payload.get("course"), _course_label_map([payload.get("course")]).get(payload.get("course")))
	if payload.get("weekly_timeslot"):
		payload["weekly_timeslot_detail"] = _get_timeslot_summary(payload.get("weekly_timeslot"))
	if payload.get("invoice"):
		payload["invoice_summary"] = _get_invoice_summary(payload.get("invoice"))
	return payload


def _get_invoice_summary(invoice):
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		return None
	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "posting_date", "due_date", "status", "docstatus", "grand_total", "outstanding_amount"],
	)
	rows = frappe.get_all("Sales Invoice", filters={"name": invoice}, fields=fields, limit=1)
	return _invoice_row_payload(rows[0]) if rows else None


def _get_course_session_rows(
	weekly_timeslot=None,
	term=None,
	course=None,
	campus=None,
	from_date=None,
	to_date=None,
	status=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=160,
):
	if not _doctype_available("Course Sessions"):
		return []
	filters = {}
	if weekly_timeslot:
		filters["weekly_timeslot"] = weekly_timeslot
	if status:
		filters["status"] = status
	if from_date and to_date:
		filters["session_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["session_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["session_date"] = ["<=", getdate(to_date)]
	timeslot_ids = _filter_timeslots_for_session_query(
		term=term,
		course=course,
		campus=campus,
		include_inactive_terms=include_inactive_terms,
		include_inactive_timeslots=include_inactive_timeslots,
	)
	if timeslot_ids is not None:
		if weekly_timeslot and weekly_timeslot not in timeslot_ids:
			return []
		if not weekly_timeslot:
			filters["weekly_timeslot"] = ["in", timeslot_ids]
	fields = _safe_fields(
		"Course Sessions",
		[
			"name",
			"weekly_timeslot",
			"session_date",
			"status",
			"teacher_override",
			"revenue_share_override",
			"revenue_share_teacher",
			"revenue_share_percent",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Course Sessions",
		filters=filters,
		fields=fields,
		order_by="session_date asc, modified asc",
		limit=limit,
	)
	timeslot_map = _get_timeslot_map([row.weekly_timeslot for row in rows if row.get("weekly_timeslot")])
	student_counts = _get_course_session_student_counts([row.get("name") for row in rows])
	trial_counts = _get_course_session_trial_counts([row.get("name") for row in rows])
	leave_counts = _get_course_session_leave_counts([row.get("name") for row in rows])
	items = []
	for row in rows:
		item = _normalize_row_payload("Course Sessions", row)
		item["weekly_timeslot_detail"] = timeslot_map.get(row.weekly_timeslot)
		timeslot_teacher = (item.get("weekly_timeslot_detail") or {}).get("teacher")
		item["teacher"] = item.get("teacher_override") or timeslot_teacher
		item["teacher_assignment_source"] = "Session override" if item.get("teacher_override") else "Weekly timeslot"
		item["student_count"] = student_counts.get(row.get("name"), 0)
		item["trial_count"] = trial_counts.get(row.get("name"), 0)
		item["leave_count"] = leave_counts.get(row.get("name"), 0)
		if item.get("weekly_timeslot_detail"):
			_attach_course_label(item, item["weekly_timeslot_detail"].get("course"), item["weekly_timeslot_detail"])
		items.append(item)
	return sorted(items, key=_course_session_sort_key)


def _course_session_sort_key(item):
	detail = item.get("weekly_timeslot_detail") or {}
	start_time = detail.get("start_time") or item.get("start_time")
	time_key = (1, 0)
	if start_time:
		try:
			parsed_time = get_time(start_time)
			time_key = (0, parsed_time.hour * 3600 + parsed_time.minute * 60 + parsed_time.second)
		except (TypeError, ValueError):
			pass
	return (
		str(item.get("session_date") or "9999-12-31"),
		*time_key,
		str(detail.get("campus") or item.get("campus") or "").casefold(),
		str(detail.get("course") or item.get("course") or "").casefold(),
		str(item.get("name") or "").casefold(),
	)


def _get_course_session_student_counts(course_sessions):
	course_sessions = sorted({course_session for course_session in course_sessions if course_session})
	if not course_sessions or not _doctype_available(ATTENDANCE_DOCTYPE):
		return {}
	filters = {"course_session": ["in", course_sessions]}
	if _has_field(ATTENDANCE_DOCTYPE, "status"):
		filters["status"] = ["not in", sorted(NON_ATTENDING_ATTENDANCE_STATUSES)]
	rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters=filters,
		fields=["course_session", "count(name) as student_count"],
		group_by="course_session",
		limit_page_length=0,
	)
	return {row.get("course_session"): cint(row.get("student_count")) for row in rows}


def _get_course_session_trial_counts(course_sessions):
	course_sessions = sorted({course_session for course_session in course_sessions if course_session})
	if not course_sessions or not _doctype_available(ATTENDANCE_DOCTYPE) or not _has_field(ATTENDANCE_DOCTYPE, "source_doctype"):
		return {}
	filters = {
		"course_session": ["in", course_sessions],
		"source_doctype": "Inquiry",
	}
	if _has_field(ATTENDANCE_DOCTYPE, "status"):
		filters["status"] = ["not in", sorted(NON_ATTENDING_ATTENDANCE_STATUSES)]
	rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters=filters,
		fields=["course_session", "count(name) as trial_count"],
		group_by="course_session",
		limit_page_length=0,
	)
	return {row.get("course_session"): cint(row.get("trial_count")) for row in rows}


def _get_course_session_leave_counts(course_sessions):
	course_sessions = sorted({course_session for course_session in course_sessions if course_session})
	if not course_sessions or not _doctype_available(ATTENDANCE_DOCTYPE) or not _has_field(ATTENDANCE_DOCTYPE, "status"):
		return {}
	filters = {
		"course_session": ["in", course_sessions],
		"status": "Leave",
	}
	rows = frappe.get_all(
		ATTENDANCE_DOCTYPE,
		filters=filters,
		fields=["course_session", "count(name) as leave_count"],
		group_by="course_session",
		limit_page_length=0,
	)
	return {row.get("course_session"): cint(row.get("leave_count")) for row in rows}


def _apply_weekly_timeslot_payload(doc, payload):
	for fieldname in [
		"term",
		"course",
		"class_language",
		"campus",
		"classroom",
		"teacher",
		"day_of_week",
		"start_time",
		"end_time",
		"status",
		"revenue_share_enabled",
		"revenue_share_teacher",
		"revenue_share_percent",
	]:
		if fieldname in payload:
			_set_if_field(doc, fieldname, payload.get(fieldname))
	if "end_time" not in payload and (not doc.get("end_time") or "course" in payload or "start_time" in payload):
		_apply_course_duration_end_time(doc)
	_validate_weekly_timeslot_room_conflict(doc)


def _weekly_timeslot_has_course_sessions(weekly_timeslot):
	return bool(weekly_timeslot and frappe.db.exists("Course Sessions", {"weekly_timeslot": weekly_timeslot}))


def _assert_active_teacher(teacher):
	if not teacher or not frappe.db.exists("Teacher", teacher):
		frappe.throw(_("An active teacher is required."))
	if _has_field("Teacher", "status") and frappe.db.get_value("Teacher", teacher, "status") != "Active":
		frappe.throw(_("Only active teachers can be assigned to classes."))


def validate_weekly_timeslot_document(doc, method=None):
	if not doc.get("end_time") and doc.get("course") and doc.get("start_time"):
		_apply_course_duration_end_time(doc)
	_validate_weekly_timeslot_room_conflict(doc)


def _apply_course_duration_end_time(doc):
	course = doc.get("course")
	start_time = doc.get("start_time")
	if not course or not start_time:
		return
	duration = cint(frappe.db.get_value("Course", course, "duration_mins")) if frappe.db.exists("Course", course) else 0
	if not duration:
		frappe.throw(_("Course duration is missing for {0}. Set the course duration or provide an end time.").format(course))
	_set_if_field(doc, "end_time", _add_minutes_to_time(start_time, duration))


def _add_minutes_to_time(start_time, minutes):
	total_minutes = _time_to_minutes(start_time, _("Start time is invalid."))
	total_minutes = (total_minutes + cint(minutes)) % (24 * 60)
	return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _validate_weekly_timeslot_room_conflict(doc):
	if (doc.get("status") or "Active") != "Active":
		return
	if not all(doc.get(fieldname) for fieldname in ["term", "classroom", "day_of_week", "start_time", "end_time"]):
		return
	start_minutes = _time_to_minutes(doc.get("start_time"), _("Start time is invalid."))
	end_minutes = _time_to_minutes(doc.get("end_time"), _("End time is invalid."))
	if end_minutes <= start_minutes:
		frappe.throw(_("Weekly timeslot end time must be after start time."))
	filters = {
		"term": doc.get("term"),
		"classroom": doc.get("classroom"),
		"day_of_week": doc.get("day_of_week"),
		"status": "Active",
	}
	fields = _safe_fields("Weekly Timeslot", ["name", "course", "classroom", "teacher", "day_of_week", "start_time", "end_time", "status"])
	for row in frappe.get_all("Weekly Timeslot", filters=filters, fields=fields, limit_page_length=0):
		if row.name == doc.name or not row.get("start_time") or not row.get("end_time"):
			continue
		row_start = _time_to_minutes(row.start_time, _("Start time is invalid."))
		row_end = _time_to_minutes(row.end_time, _("End time is invalid."))
		if row_end <= row_start:
			continue
		if start_minutes < row_end and row_start < end_minutes:
			frappe.throw(
				_("Room {0} is already occupied by {1} on {2} {3}-{4}. Please choose another room or time.").format(
					doc.get("classroom"),
					row.get("course") or row.name,
					doc.get("day_of_week"),
					_format_time_for_message(row.start_time),
					_format_time_for_message(row.end_time),
				)
			)


def _time_to_minutes(value, error_message):
	if hasattr(value, "total_seconds"):
		return int(value.total_seconds() // 60)
	if hasattr(value, "hour") and hasattr(value, "minute"):
		return value.hour * 60 + value.minute
	time_parts = str(value or "").split(".")[0].split(":")
	if len(time_parts) < 2:
		frappe.throw(error_message)
	return cint(time_parts[0]) * 60 + cint(time_parts[1])


def _format_time_for_message(value):
	if hasattr(value, "total_seconds"):
		total_minutes = int(value.total_seconds() // 60)
		return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
	if hasattr(value, "hour") and hasattr(value, "minute"):
		return f"{value.hour:02d}:{value.minute:02d}"
	return str(value or "")[:5]


def _weekday_number(day_of_week):
	lookup = {
		"Monday": 0,
		"Tuesday": 1,
		"Wednesday": 2,
		"Thursday": 3,
		"Friday": 4,
		"Saturday": 5,
		"Sunday": 6,
	}
	if day_of_week not in lookup:
		frappe.throw(_("Weekly timeslot day of week is required."))
	return lookup[day_of_week]


def _ensure_course_session(weekly_timeslot, session_date):
	existing = frappe.db.exists(
		"Course Sessions",
		{"weekly_timeslot": weekly_timeslot, "session_date": getdate(session_date)},
	)
	if existing:
		return {"name": existing, "created": False}
	session = frappe.new_doc("Course Sessions")
	session.weekly_timeslot = weekly_timeslot
	session.session_date = getdate(session_date)
	session.status = "Scheduled"
	session.insert(ignore_permissions=True)
	_create_session_attendance_for_active_enrollments(session)
	return {"name": session.name, "created": True}


def _sync_future_course_sessions_for_timeslot(doc, effective_date=None, previous_day_of_week=None):
	if not _doctype_available("Course Sessions"):
		return {"updated": 0, "skipped": 0, "checked": 0, "reason": "Course Sessions is not installed."}
	start_date = getdate(effective_date or today())
	filters = {
		"weekly_timeslot": doc.name,
		"session_date": [">=", start_date],
		"status": ["not in", ["Completed", "Cancelled"]],
	}
	sessions = frappe.get_all(
		"Course Sessions",
		filters=filters,
		fields=["name", "session_date", "status"],
		order_by="session_date asc, modified asc",
		limit_page_length=0,
	)
	if not sessions:
		return {"updated": 0, "skipped": 0, "checked": 0}

	if not doc.get("day_of_week") or previous_day_of_week == doc.get("day_of_week"):
		return {"updated": 0, "skipped": 0, "checked": len(sessions), "reason": "Session dates already match this weekday."}

	target_dates = _future_weekday_dates(start_date, doc.get("day_of_week"), len(sessions))
	updated = 0
	skipped = 0
	skipped_sessions = []
	for session, target_date in zip(sessions, target_dates):
		current_date = getdate(session.get("session_date"))
		if current_date == target_date:
			continue
		if _course_session_date_exists(doc.name, target_date, exclude=session.name):
			skipped += 1
			skipped_sessions.append({"session": session.name, "reason": _("Target date already has a session."), "target_date": str(target_date)})
			continue
		if _course_session_has_locked_attendance(session.name):
			skipped += 1
			skipped_sessions.append({"session": session.name, "reason": _("Attendance has already been marked."), "target_date": str(target_date)})
			continue
		frappe.db.set_value("Course Sessions", session.name, "session_date", target_date, update_modified=True)
		updated += 1
	return {
		"updated": updated,
		"skipped": skipped,
		"checked": len(sessions),
		"effective_date": str(start_date),
		"day_of_week": doc.get("day_of_week"),
		"skipped_sessions": skipped_sessions,
	}


def _future_weekday_dates(start_date, day_of_week, count):
	target_weekday = _weekday_number(day_of_week)
	current = getdate(start_date)
	while current.weekday() != target_weekday:
		current = current + timedelta(days=1)
	dates = []
	while len(dates) < count:
		dates.append(current)
		current = current + timedelta(days=7)
	return dates


def _course_session_date_exists(weekly_timeslot, session_date, exclude=None):
	filters = {"weekly_timeslot": weekly_timeslot, "session_date": getdate(session_date)}
	existing = frappe.db.exists("Course Sessions", filters)
	return bool(existing and existing != exclude)


def _course_session_has_locked_attendance(course_session):
	if not _doctype_available("Class Attendance Entry"):
		return False
	return bool(frappe.db.exists(
		"Class Attendance Entry",
		{
			"course_session": course_session,
			"status": ["not in", ["To be started", "Cancelled"]],
		},
	))


def _create_session_attendance_for_active_enrollments(session):
	rows = frappe.get_all(
		"Enrollment",
		filters={
			"weekly_timeslot": session.weekly_timeslot,
			"status": "Active",
			"enrollment_type": "Full-Term",
		},
		fields=["name", "student"],
		limit_page_length=0,
	)
	for enrollment in rows:
		if enrollment.get("student"):
			create_full_term_attendance_entries([session], enrollment.student, enrollment.name)


def _filter_timeslots_for_session_query(
	term=None,
	course=None,
	campus=None,
	include_inactive_terms=0,
	include_inactive_timeslots=0,
):
	if not _doctype_available("Weekly Timeslot"):
		return []
	filters = {}
	for fieldname, value in {"term": term, "course": course, "campus": campus}.items():
		if value and _has_field("Weekly Timeslot", fieldname):
			filters[fieldname] = value
	_apply_active_term_filter(filters, term=term, include_inactive_terms=include_inactive_terms)
	_apply_active_timeslot_filter(filters, include_inactive_timeslots=include_inactive_timeslots)
	if not filters:
		return None
	return [row.name for row in frappe.get_all("Weekly Timeslot", filters=filters, fields=["name"])]


def _get_timeslot_map(timeslot_ids):
	if not _doctype_available("Weekly Timeslot"):
		return {}
	timeslot_ids = sorted({timeslot_id for timeslot_id in timeslot_ids if timeslot_id})
	if not timeslot_ids:
		return {}
	fields = _safe_fields(
		"Weekly Timeslot",
		[
			"name",
			"term",
			"course",
			"class_language",
			"campus",
			"classroom",
			"teacher",
			"day_of_week",
			"start_time",
			"end_time",
			"status",
			"revenue_share_enabled",
			"revenue_share_teacher",
			"revenue_share_percent",
		],
	)
	rows = frappe.get_all("Weekly Timeslot", filters={"name": ["in", timeslot_ids]}, fields=fields)
	items = [_normalize_row_payload("Weekly Timeslot", row) for row in rows]
	_attach_course_labels(items)
	return {row.get("name"): row for row in items}


def _get_timeslot_summary(weekly_timeslot):
	return _get_timeslot_map([weekly_timeslot]).get(weekly_timeslot)


def _add_comment(reference_doctype, reference_name, content):
	try:
		comment = frappe.new_doc("Comment")
		comment.comment_type = "Info"
		comment.reference_doctype = reference_doctype
		comment.reference_name = reference_name
		comment.content = content
		comment.comment_by = frappe.session.user
		comment.insert(ignore_permissions=True)
	except Exception:
		pass


def _get_comments(reference_doctype, reference_name, limit=20):
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		},
		fields=["name", "comment_type", "content", "comment_by", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	return [_normalize_row_payload("Comment", row) for row in rows]


def _set_if_field(doc, fieldname, value):
	if fieldname and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)
