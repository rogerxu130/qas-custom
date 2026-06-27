from __future__ import annotations

import frappe
from frappe import _


FINAL_INQUIRY_STATUSES = {"Converted", "Cancelled", "Inactive"}


def assert_trial_convertible(inquiry_doc):
	if inquiry_doc.inquiry_type != "Trial Lesson":
		frappe.throw(_("Only trial lesson inquiries can be converted."))
	if inquiry_doc.status not in {"Completed", "Follow-up"}:
		frappe.throw(_("Only completed trial lessons can be converted."))
	if inquiry_doc.converted_enrollment:
		frappe.throw(_("This inquiry has already been converted."))
	if not inquiry_doc.student:
		frappe.throw(_("Student is required before converting a trial lesson."))
	if not inquiry_doc.parent:
		frappe.throw(_("Parent is required before converting a trial lesson."))


def get_inquiry_for_conversion(inquiry: str | None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	assert_trial_convertible(inquiry_doc)
	return inquiry_doc


def mark_converted(inquiry_doc, enrollment, invoice):
	inquiry_doc.status = "Converted"
	inquiry_doc.converted_enrollment = enrollment.name
	if inquiry_doc.meta.has_field("converted_invoice"):
		inquiry_doc.converted_invoice = invoice.name
	inquiry_doc.save(ignore_permissions=True)
	return inquiry_doc


def sync_trial_inquiry_from_attendance(inquiry: str, target_status: str):
	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.inquiry_type != "Trial Lesson":
		return {"inquiry": inquiry_doc, "updated": False, "skipped": True}

	if inquiry_doc.status in FINAL_INQUIRY_STATUSES or inquiry_doc.status == "Follow-up":
		return {"inquiry": inquiry_doc, "updated": False, "skipped": False}

	if inquiry_doc.status != target_status:
		inquiry_doc.status = target_status
		inquiry_doc.save(ignore_permissions=True)
		return {"inquiry": inquiry_doc, "updated": True, "skipped": False}

	return {"inquiry": inquiry_doc, "updated": False, "skipped": False}


def mark_inquiry_inactive_core(inquiry: str | None, inactive_reason: str | None, actor=None):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	reason = (inactive_reason or "").strip()
	if not reason:
		frappe.throw(_("Inactive reason is required."))

	inquiry_doc = frappe.get_doc("Inquiry", inquiry)
	if inquiry_doc.status == "Converted":
		frappe.throw(_("A converted inquiry cannot be marked inactive."))
	if inquiry_doc.status not in {"Completed", "Follow-up", "No-show"}:
		frappe.throw(_("Only post-visit inquiries can be marked inactive."))

	inquiry_doc.status = "Inactive"
	inquiry_doc.inactive_reason = reason
	inquiry_doc.save(ignore_permissions=True)
	frappe.db.commit()

	from qas_custom.services.inquiry import build_inquiry_detail

	return build_inquiry_detail(inquiry_doc.name)
