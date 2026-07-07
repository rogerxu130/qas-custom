import frappe

from qas_custom.services.portal_invites import (
	bulk_invite_parents_to_portal_data,
	bulk_invite_teachers_to_portal_data,
	invite_parent_to_portal_data,
	invite_teacher_to_portal_data,
)


@frappe.whitelist()
def school_admin_invite_parent_to_portal(parent=None):
	return invite_parent_to_portal_data(parent=parent)


@frappe.whitelist()
def school_admin_bulk_invite_parents_to_portal(payload=None):
	return bulk_invite_parents_to_portal_data(payload=payload)


@frappe.whitelist()
def school_admin_invite_teacher_to_portal(teacher=None):
	return invite_teacher_to_portal_data(teacher=teacher)


@frappe.whitelist()
def school_admin_bulk_invite_teachers_to_portal(payload=None):
	return bulk_invite_teachers_to_portal_data(payload=payload)
