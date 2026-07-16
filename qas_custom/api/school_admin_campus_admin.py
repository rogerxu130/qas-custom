import frappe

from qas_custom.services.campus_admin_accounts import (
	get_campus_admin_account_data,
	get_campus_admin_accounts_data,
	invite_campus_admin_account_data,
	save_campus_admin_account_data,
	set_campus_admin_account_active_data,
)


@frappe.whitelist()
def school_admin_get_campus_admins(query=None):
	return get_campus_admin_accounts_data(query=query)


@frappe.whitelist()
def school_admin_get_campus_admin(profile=None):
	return get_campus_admin_account_data(profile=profile)


@frappe.whitelist()
def school_admin_save_campus_admin(profile=None, payload=None):
	return save_campus_admin_account_data(profile=profile, payload=payload)


@frappe.whitelist()
def school_admin_invite_campus_admin(profile=None):
	return invite_campus_admin_account_data(profile=profile)


@frappe.whitelist()
def school_admin_set_campus_admin_active(profile=None, active=1):
	return set_campus_admin_account_active_data(profile=profile, active=active)
