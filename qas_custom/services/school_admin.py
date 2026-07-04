from __future__ import annotations

from datetime import timedelta
import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate, today

from qas_custom.services.billing_enrollment import (
	convert_inquiry_to_full_term_core,
	get_conversion_session_options,
	mark_inquiry_inactive_core,
)
from qas_custom.modules.attendance.commands import create_full_term_attendance_entries, update_attendance_status
from qas_custom.modules.billing.store_credit import (
	adjust_store_credit,
	apply_store_credit_to_invoice,
	cancel_store_credit_journal_entries,
	create_store_credit_entry,
	get_invoice_payable_amount,
	get_invoice_store_credit_applied,
	get_store_credit_summary,
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
from qas_custom.modules.notifications import (
	get_invoice_notification_summary,
	maybe_send_parent_invoice_paid_receipt,
	parent_portal_invoice_link,
	send_parent_invoice_notification,
)
from qas_custom.modules.notifications.guard import disable_sales_invoice_auto_notifications
from qas_custom.services.class_attendance import get_attendance_entries
from qas_custom.services.display_labels import get_course_session_snapshot_label, get_student_display_code, get_student_display_name, get_student_parent_name
from qas_custom.utils.environment import payment_block_reason, payment_mutations_enabled
from qas_custom.services.inquiry import (
	add_inquiry_note_core,
	build_inquiry_detail,
	build_inquiry_summary,
	mark_inquiry_status_core,
	reschedule_inquiry_core,
)
from qas_custom.services.teacher_revenue_share import get_teacher_revenue_share_session_rows


ADMIN_ROLES = {"School Admin", "System Manager"}
INQUIRY_OPEN_STATUSES = ["New", "Needs Review", "Booked", "Rescheduled", "No-show"]
INQUIRY_POST_VISIT_STATUSES = ["Completed", "Follow-up"]
ACTIVE_TERM_STATUSES = ["Upcoming", "Active"]
ACTIVE_TIMESLOT_STATUSES = ["Active"]
COURSE_LABEL_FIELDS = ["name", "course_name", "course_name_zh"]
DEFAULT_COURSE_INVOICE_ITEM = "Tuition Fee"
PARENT_EDIT_FIELDS = ["parent_name", "mobile_number", "phone", "email", "email_id", "address", "status", "customer"]
STUDENT_EDIT_FIELDS = ["student_name", "first_name", "last_name", "date_of_birth", "dob", "gender", "status", "guardian", "parent"]
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
		"fields": ["name", "teacher_name", "status", "email", "mobile", "phone", "notes", "modified"],
		"edit_fields": ["teacher_name", "status", "email", "mobile", "phone", "notes"],
		"required": ["teacher_name"],
		"search_fields": ["name", "teacher_name", "email", "mobile", "phone"],
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


def get_school_admin_dashboard_data():
	_require_school_admin()
	start_date = getdate(today())
	end_date = getdate(add_days(start_date, 7))
	outstanding = _get_outstanding_invoice_summary()
	active_enrollment_filters = {"status": "Active"}
	_apply_active_term_filter(active_enrollment_filters)
	return {
		"date": str(start_date),
		"action_counts": {
			"draft_invoices": _count_sales_invoices({"docstatus": 0}),
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
	_apply_master_payload(doc, _get_payload(payload), PARENT_EDIT_FIELDS)
	_validate_required(doc, ["parent_name"])
	doc.save(ignore_permissions=True)
	_add_comment("Parent", doc.name, _("Parent updated by School Admin."))
	frappe.db.commit()
	return get_school_admin_family_data(parent=doc.name)


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
	fields = _safe_fields("Student", ["name", *STUDENT_EDIT_FIELDS, "modified"])
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
	)
	items = []
	for row in rows:
		item = _normalize_row_payload("Item", row)
		value = item.get("name") or item.get("item_code")
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
	entry = adjust_store_credit(
		parent=parent,
		customer=customer,
		amount=amount,
		reason=reason,
		notes=notes,
	)
	frappe.db.commit()
	return {
		"entry": entry.as_dict(),
		"store_credit": get_store_credit_summary(parent=entry.parent, customer=entry.customer, limit=50),
	}


def get_school_admin_inquiries_data(
	status=None,
	inquiry_type=None,
	campus=None,
	from_date=None,
	to_date=None,
	queue=None,
	limit=80,
):
	_require_school_admin()
	filters = {}
	if status:
		filters["status"] = status
	elif queue == "post_visit":
		filters["status"] = ["in", INQUIRY_POST_VISIT_STATUSES]
	elif queue == "upcoming":
		filters["status"] = ["in", INQUIRY_OPEN_STATUSES]
	elif queue == "needs_scheduling":
		filters["status"] = "Needs Review"
	if inquiry_type:
		filters["inquiry_type"] = inquiry_type
	if campus:
		filters["campus"] = campus
	if from_date and to_date:
		filters["current_appointment_date"] = ["between", [getdate(from_date), getdate(to_date)]]
	elif from_date:
		filters["current_appointment_date"] = [">=", getdate(from_date)]
	elif to_date:
		filters["current_appointment_date"] = ["<=", getdate(to_date)]

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
			"course_session",
			"current_appointment_date",
			"current_appointment_time",
			"converted_enrollment",
			"converted_invoice",
			"modified",
		],
	)
	rows = frappe.get_all(
		"Inquiry",
		filters=filters,
		fields=fields,
		order_by=_inquiry_order_by(queue),
		limit=_limit(limit, default=80, max_value=200),
	)
	return {"items": [_build_inquiry_list_item(row) for row in rows]}


def get_school_admin_inquiry_data(inquiry=None):
	_require_school_admin()
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	return build_inquiry_detail(inquiry)


def add_school_admin_inquiry_note_data(inquiry=None, note=None):
	_require_school_admin()
	return add_inquiry_note_core(inquiry, note, actor=frappe.session.user)


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
	_set_if_field(invoice, "remarks", payload.get("remarks"))
	_apply_invoice_payment_payload(invoice, payload)
	apply_invoice_payment_snapshot(invoice)
	_apply_invoice_items(invoice, items)
	_sync_invoice_student_summary(invoice)
	invoice.insert(ignore_permissions=True)
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
	_apply_invoice_payment_payload(doc, payload)
	apply_invoice_payment_snapshot(doc)
	if "items" in payload:
		_apply_invoice_items(doc, payload.get("items") or [])
	_sync_invoice_student_summary(doc)
	doc.save(ignore_permissions=True)
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
	_clear_deleted_invoice_enrollment_snapshot(doc)
	deleted = doc.name
	frappe.delete_doc("Sales Invoice", deleted, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": deleted}


def submit_school_admin_invoice_data(invoice=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft invoices can be submitted."))
	if apply_invoice_payment_snapshot(doc):
		doc.save(ignore_permissions=True)
	doc.flags.ignore_permissions = True
	doc.submit()
	_add_comment("Sales Invoice", doc.name, "Invoice approved and submitted by School Admin.")
	application = apply_store_credit_to_invoice(doc)
	if flt(application.get("applied")) > 0:
		_add_comment("Sales Invoice", doc.name, _("Store credit applied: {0}.").format(flt(application.get("applied"))))
	doc = frappe.get_doc("Sales Invoice", doc.name)
	sync_invoice_store_credit_snapshot(doc)
	applied_amount = flt(application.get("applied"))
	frappe.db.commit()
	doc = frappe.get_doc("Sales Invoice", doc.name)
	notification = _send_invoice_notification(doc, event="approved", store_credit_applied=applied_amount if applied_amount > 0 else None)
	receipt_notification = _maybe_send_paid_receipt(doc, source="invoice_submit")
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
		doc.save(ignore_permissions=True)
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
	_add_comment(
		"Sales Invoice",
		doc.name,
		_("Payment recorded by School Admin: {0}.").format(payment_entry.name),
	)
	sync_invoice_store_credit_snapshot(doc.name)
	frappe.db.commit()
	doc = frappe.get_doc("Sales Invoice", invoice)
	receipt_notification = _maybe_send_paid_receipt(doc, payment_entry=payment_entry, source="mark_paid")
	frappe.db.commit()
	payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))
	payload["receipt_notification"] = receipt_notification
	payload["payment_entry"] = payment_entry.name
	return payload


def cancel_school_admin_invoice_data(invoice=None, reason=None):
	_require_school_admin()
	if not invoice:
		frappe.throw(_("Invoice is required."))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Cancellation reason is required."))

	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) == 2:
		return _build_invoice_payload(doc)
	if cint(doc.docstatus) == 1:
		if not payment_mutations_enabled():
			frappe.throw(_(payment_block_reason()))
		paid_credit_amount = _invoice_payment_amount(doc.name)
		_cancel_invoice_payment_entries(doc.name)
		cancel_store_credit_journal_entries(doc.name)
		_reverse_invoice_store_credit_application(doc, reason)
		paid_credit = _create_invoice_cancellation_store_credit(doc, paid_credit_amount, reason)
		_cancel_submitted_invoice_as_admin(doc.name)
		_add_comment("Sales Invoice", doc.name, f"Invoice cancelled by School Admin. Reason: {reason}")
		frappe.db.commit()
		payload = _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))
		payload["cancellation_store_credit_amount"] = paid_credit_amount if paid_credit else 0
		payload["cancellation_store_credit"] = paid_credit.name if paid_credit else None
		return payload

	_mark_draft_invoice_cancelled(doc, reason)
	frappe.db.commit()
	return _build_invoice_payload(frappe.get_doc("Sales Invoice", invoice))


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

	results = []
	for invoice in invoices:
		invoice_name = (invoice or "").strip()
		if not invoice_name:
			continue
		try:
			if action == "submit":
				result = submit_school_admin_invoice_data(invoice=invoice_name)
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
	created_invoices = 0
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
			if result.get("invoice_created"):
				created_invoices += 1
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
			"created_invoices": created_invoices,
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
	_create_enrollment_attendance_entries(enrollment)
	invoice = _create_term_enrollment_invoice(enrollment, start_session)
	if invoice:
		_set_if_field(enrollment, "invoice", invoice.name)
		_set_if_field(enrollment, "invoice_status", "Draft")
		_set_if_field(enrollment, "invoice_amount", invoice.get("grand_total"))
		enrollment.save(ignore_permissions=True)
	_add_comment("Enrollment", enrollment.name, _("Planned enrollment activated for term {0}.").format(term_doc.name))
	return {
		"enrollment": enrollment.name,
		"invoice": invoice.name if invoice else None,
		"invoice_created": bool(invoice and getattr(invoice.flags, "qas_was_created", False)),
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
	unit_rate = flt(full_term_fee) / flt(total_sessions)

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
		unit_rate=unit_rate,
	)
	_sync_invoice_student_summary(invoice)
	apply_invoice_payment_snapshot(invoice)
	if created:
		invoice.insert(ignore_permissions=True)
	else:
		invoice.save(ignore_permissions=True)
	invoice.flags.qas_was_created = created
	return invoice


def _find_draft_family_invoice(parent, customer, term):
	if not customer or not term or not _doctype_available("Sales Invoice"):
		return None

	filters = {"customer": customer, "docstatus": 0}
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


def _append_enrollment_invoice_item(invoice, *, enrollment, start_session, item_code, course, session_count, unit_rate):
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
			"qty": session_count,
			"rate": unit_rate,
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
	if doc.get("status") == "Active":
		_create_enrollment_attendance_entries(doc)
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
	_apply_enrollment_payload(doc, payload)
	_validate_unique_open_enrollment(doc)
	doc.save(ignore_permissions=True)
	if doc.get("status") == "Active" and payload.get("weekly_timeslot") and payload.get("weekly_timeslot") != previous_timeslot:
		_cancel_future_enrollment_attendance(doc.name, effective_date=payload.get("effective_date") or today())
		_create_enrollment_attendance_entries(doc, start_date=payload.get("effective_date") or today())
	elif doc.get("status") == "Active" and previous_status != "Active":
		_create_enrollment_attendance_entries(doc, start_date=payload.get("effective_date") or today())
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
	if doc.get("invoice"):
		frappe.throw(_("This enrollment already has an invoice."))
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
		"invoice": result.get("invoice"),
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
	start_session = _validate_enrollment_start_session(
		payload.get("start_course_session") or doc.get("start_course_session"),
		doc.weekly_timeslot,
		term_doc,
	)
	timeslot_course = frappe.db.get_value("Weekly Timeslot", doc.weekly_timeslot, "course")
	_set_if_field(doc, "start_course_session", start_session)
	_set_if_field(doc, "course", doc.get("course") or timeslot_course)
	if not doc.get("enrollment_date"):
		start_date = frappe.db.get_value("Course Sessions", start_session, "session_date")
		_set_if_field(doc, "enrollment_date", start_date or term_doc.get("start_date") or today())
	doc.save(ignore_permissions=True)

	invoice = _create_term_enrollment_invoice(doc, start_session)
	_set_if_field(doc, "invoice", invoice.name)
	_set_if_field(doc, "invoice_status", "Draft")
	_set_if_field(doc, "invoice_amount", invoice.get("grand_total"))
	doc.save(ignore_permissions=True)
	_add_comment("Enrollment", doc.name, _("Draft invoice {0} created by School Admin.").format(invoice.name))
	frappe.db.commit()
	return {"enrollment": _build_enrollment_payload(doc), "invoice": invoice.name}


def transfer_school_admin_enrollment_data(enrollment=None, payload=None):
	_require_school_admin()
	if not enrollment:
		frappe.throw(_("Enrollment is required."))
	payload = _get_payload(payload)
	target_timeslot = payload.get("weekly_timeslot")
	if not target_timeslot:
		frappe.throw(_("Target weekly timeslot is required."))
	doc = frappe.get_doc("Enrollment", enrollment)
	effective_date = payload.get("effective_date") or today()
	_cancel_future_enrollment_attendance(doc.name, effective_date=effective_date)
	doc.weekly_timeslot = target_timeslot
	target_course = payload.get("course") or frappe.db.get_value("Weekly Timeslot", target_timeslot, "course")
	target_term = payload.get("term") or frappe.db.get_value("Weekly Timeslot", target_timeslot, "term")
	_set_if_field(doc, "course", target_course)
	_set_if_field(doc, "term", target_term)
	_set_if_field(doc, "start_course_session", payload.get("start_course_session"))
	if _has_field("Enrollment", "status"):
		doc.status = payload.get("status") or "Active"
	_validate_unique_open_enrollment(doc)
	doc.save(ignore_permissions=True)
	_create_enrollment_attendance_entries(doc, start_date=effective_date)
	_add_comment("Enrollment", doc.name, _("Enrollment transferred to {0} by School Admin.").format(target_timeslot))
	frappe.db.commit()
	return _build_enrollment_payload(doc)


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
			from_date=from_date,
			to_date=to_date,
			include_inactive_terms=include_inactive_terms,
			include_inactive_timeslots=include_inactive_timeslots,
			limit=_limit(limit, default=160, max_value=300),
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
	payload["attendance"] = _get_school_admin_attendance_rows(course_session)
	if payload.get("weekly_timeslot"):
		payload["weekly_timeslot_detail"] = _get_timeslot_summary(payload.get("weekly_timeslot"))
	return payload


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


def _get_school_admin_attendance_rows(course_session):
	rows = [_docdict(row) for row in get_attendance_entries([course_session])]
	for row in rows:
		student = row.get("student")
		row["student_display"] = get_student_display_name(student) or student
		row["student_code"] = get_student_display_code(student) or student
		row["attendance_type"] = row.get("enrollment_type") or _infer_attendance_type(row)
		row["source_label"] = _attendance_source_label(row)
	return rows


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
	return payload


def _normalize_school_setup_record(doc, config):
	if _has_field(doc.doctype, "status"):
		status = doc.get("status")
		if not status:
			doc.set("status", "Active")
		elif status not in {"Active", "Inactive"}:
			frappe.throw(_("Status must be Active or Inactive."))
	if doc.doctype == "Classroom" and doc.get("campus") and not frappe.db.exists("Campus", doc.get("campus")):
		frappe.throw(_("Campus does not exist: {0}").format(doc.get("campus")))



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

	rows = frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", invoice_names], "docstatus": ["!=", 2]},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None


def _existing_invoice_for_enrollment(enrollment_doc):
	invoice = enrollment_doc.get("invoice")
	if invoice and frappe.db.exists("Sales Invoice", invoice):
		return invoice
	if not _doctype_available("Sales Invoice"):
		return None
	if _has_field("Sales Invoice", "enrollment"):
		rows = frappe.get_all(
			"Sales Invoice",
			filters={"enrollment": enrollment_doc.name, "docstatus": ["!=", 2]},
			pluck="name",
			limit=1,
		)
		if rows:
			return rows[0]
	if _has_field("Sales Invoice", "source_doctype") and _has_field("Sales Invoice", "source_document"):
		rows = frappe.get_all(
			"Sales Invoice",
			filters={"source_doctype": "Enrollment", "source_document": enrollment_doc.name, "docstatus": ["!=", 2]},
			pluck="name",
			limit=1,
		)
		if rows:
			return rows[0]
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
			data[field.fieldname] = [_child_payload(row) for row in doc.get(field.fieldname, [])]
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
	fields = _safe_fields(
		"Sales Invoice",
		["name", "customer", "posting_date", "due_date", "status", "docstatus", "grand_total", "outstanding_amount"],
	)
	return _search_doctype("Sales Invoice", query, fields, ["name", "customer", "status"], limit)


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
	fields = _safe_fields("Student", ["name", *STUDENT_EDIT_FIELDS, "modified"])
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
	if queue == "post_visit":
		return "current_appointment_date desc, modified desc"
	return "current_appointment_date asc, current_appointment_time asc, modified desc"


def _get_invoice_rows(status=None, customer=None, parent=None, students=None, source=None, limit=80):
	if not _doctype_available("Sales Invoice"):
		return []
	filters = {}
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
	return [_invoice_row_payload(row) for row in rows]


def _invoice_row_payload(row):
	payload = _normalize_row_payload("Sales Invoice", row)
	payload.update(_invoice_credit_payload(payload))
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
	elif source == "Manual" and _has_field("Sales Invoice", "source_type"):
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
	doc = frappe.get_doc("Sales Invoice", doc) if isinstance(doc, str) else doc
	payload = _document_payload(doc)
	payload["docstatus"] = cint(doc.docstatus)
	payload["status_label"] = _invoice_status_label(doc)
	payload["items"] = [_child_payload(row) for row in doc.get("items", [])]
	payload["comments"] = _get_comments("Sales Invoice", doc.name)
	payload.update(_invoice_credit_payload(doc))
	payload["notifications"] = get_invoice_notification_summary(doc.name)
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


def _mark_draft_invoice_cancelled(doc, reason):
	_add_comment("Sales Invoice", doc.name, f"Draft invoice marked cancelled by School Admin. Reason: {reason}")
	if _has_field("Sales Invoice", "status"):
		frappe.db.set_value("Sales Invoice", doc.name, "status", "Cancelled", update_modified=True)
	if _has_field("Sales Invoice", "cancel_reason"):
		frappe.db.set_value("Sales Invoice", doc.name, "cancel_reason", reason, update_modified=False)
	elif _has_field("Sales Invoice", "cancellation_reason"):
		frappe.db.set_value("Sales Invoice", doc.name, "cancellation_reason", reason, update_modified=False)


def _cancel_submitted_invoice_as_admin(invoice):
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))

	original_user = frappe.session.user
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
	original_user = frappe.session.user
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
		notes=_("Moved paid amount to store credit because invoice {0} was cancelled. Reason: {1}").format(doc.name, reason),
	)
	_add_comment("Sales Invoice", doc.name, _("Paid amount moved to store credit: {0}.").format(amount))
	return credit


def _clear_deleted_invoice_enrollment_snapshot(doc):
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
			_add_comment("Enrollment", enrollment, _("Draft invoice {0} was deleted by School Admin.").format(doc.name))


def _reverse_invoice_store_credit_application(doc, reason):
	applied = get_invoice_store_credit_applied(doc.name)
	if applied <= 0:
		return None
	parent = doc.get("parent")
	customer = doc.get("customer")
	if not customer:
		return None
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
		notes=_("Reversed store credit because invoice {0} was cancelled. Reason: {1}").format(doc.name, reason),
	)


def _send_invoice_notification(doc, event="approved", store_credit_applied=None):
	return send_parent_invoice_notification(
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


def _create_payment_entry_for_invoice(doc, amount, mode_of_payment=None, reference_no=None, notes=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))

	original_user = frappe.session.user
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
	include_inactive_terms=0,
	include_inactive_timeslots=0,
	limit=160,
):
	if not _doctype_available("Course Sessions"):
		return []
	filters = {}
	if weekly_timeslot:
		filters["weekly_timeslot"] = weekly_timeslot
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
	items = []
	for row in rows:
		item = _normalize_row_payload("Course Sessions", row)
		item["weekly_timeslot_detail"] = timeslot_map.get(row.weekly_timeslot)
		if item.get("weekly_timeslot_detail"):
			_attach_course_label(item, item["weekly_timeslot_detail"].get("course"), item["weekly_timeslot_detail"])
		items.append(item)
	return items


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
