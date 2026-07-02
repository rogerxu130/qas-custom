import frappe

from qas_custom.services.portal_invites import (
	bulk_invite_parents_to_portal_data,
	invite_parent_to_portal_data,
)


@frappe.whitelist()
def school_admin_invite_parent_to_portal(parent=None):
	return invite_parent_to_portal_data(parent=parent)


@frappe.whitelist()
def school_admin_bulk_invite_parents_to_portal(payload=None):
	return bulk_invite_parents_to_portal_data(payload=payload)
