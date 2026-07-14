import frappe

from qas_custom.services.portal_invites import (
	bulk_invite_parents_to_portal_data,
	bulk_invite_teachers_to_portal_data,
	get_term_parent_invite_job_data,
	get_term_parent_invite_preview_data,
	invite_parent_to_portal_data,
	invite_teacher_to_portal_data,
	start_term_parent_invite_job_data,
)


@frappe.whitelist()
def school_admin_invite_parent_to_portal(parent=None):
	return invite_parent_to_portal_data(parent=parent)


@frappe.whitelist()
def school_admin_bulk_invite_parents_to_portal(payload=None):
	return bulk_invite_parents_to_portal_data(payload=payload)


@frappe.whitelist()
def school_admin_get_term_parent_invite_preview(term=None, status=None):
	return get_term_parent_invite_preview_data(term=term, status=status)


@frappe.whitelist()
def school_admin_start_term_parent_invite_job(payload=None):
	return start_term_parent_invite_job_data(payload=payload)


@frappe.whitelist()
def school_admin_get_term_parent_invite_job(job_id=None):
	return get_term_parent_invite_job_data(job_id=job_id)


@frappe.whitelist()
def school_admin_invite_teacher_to_portal(teacher=None):
	return invite_teacher_to_portal_data(teacher=teacher)


@frappe.whitelist()
def school_admin_bulk_invite_teachers_to_portal(payload=None):
	return bulk_invite_teachers_to_portal_data(payload=payload)
