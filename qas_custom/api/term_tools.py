import frappe

from qas_custom.services.term_rollover import copy_term_setup_data


@frappe.whitelist()
def copy_term_setup(source_term=None, target_term=None, dry_run=1):
    return copy_term_setup_data(
        source_term=source_term,
        target_term=target_term,
        dry_run=dry_run,
    )
