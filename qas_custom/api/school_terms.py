import frappe

from qas_custom.services.school_admin import (
	copy_school_admin_term_data,
	create_school_admin_term_data,
	get_school_admin_term_data,
	get_school_admin_terms_data,
	populate_school_admin_term_data,
	populate_school_admin_term_sessions_data,
	update_school_admin_term_data,
)


@frappe.whitelist()
def school_terms_get_terms(status=None, limit=80):
	return get_school_admin_terms_data(status=status, limit=limit)


@frappe.whitelist()
def school_terms_get_term(term=None):
	return get_school_admin_term_data(term=term)


@frappe.whitelist()
def school_terms_create_term(payload=None):
	return create_school_admin_term_data(payload=payload)


@frappe.whitelist()
def school_terms_update_term(term=None, payload=None):
	return update_school_admin_term_data(term=term, payload=payload)


@frappe.whitelist()
def school_terms_copy_term(payload=None):
	return copy_school_admin_term_data(payload=payload)


@frappe.whitelist()
def school_terms_populate_term(term=None, plan=None):
	return populate_school_admin_term_data(term=term or plan)


@frappe.whitelist()
def school_terms_populate_sessions(term=None, plan=None):
	return populate_school_admin_term_sessions_data(term=term or plan)
