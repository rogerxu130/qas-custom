import frappe

from qas_custom.services.term_rollover import (
	copy_term_data,
	create_term_data,
	get_rollover_plan_data,
	get_term_data,
	get_terms_data,
	populate_term_data,
	update_rollover_plan_row_data,
)


@frappe.whitelist()
def school_terms_get_terms(status=None, limit=80):
	return get_terms_data(status=status, limit=limit)


@frappe.whitelist()
def school_terms_get_term(term=None):
	return get_term_data(term=term)


@frappe.whitelist()
def school_terms_create_term(payload=None):
	return create_term_data(payload=payload)


@frappe.whitelist()
def school_terms_copy_term(payload=None):
	return copy_term_data(payload=payload)


@frappe.whitelist()
def school_terms_get_rollover_plan(plan=None):
	return get_rollover_plan_data(plan=plan)


@frappe.whitelist()
def school_terms_update_rollover_plan_row(plan=None, row=None, payload=None):
	return update_rollover_plan_row_data(plan=plan, row=row, payload=payload)


@frappe.whitelist()
def school_terms_populate_term(plan=None):
	return populate_term_data(plan=plan)
