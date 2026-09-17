"""Provision a clearly labelled test ledger; never replay historic test payments."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from qas_custom.patches.v2026_09_17_setup_stripe_accounts import bank_parent


def execute():
	create_custom_fields({doctype: [{
		'fieldname': 'qas_stripe_test', 'label': 'Stripe Test Record', 'fieldtype': 'Check',
		'read_only': 1, 'no_copy': 1, 'allow_on_submit': 1, 'default': '0',
		'description': 'Simulated Stripe funds. Cancel test entries and invoices after acceptance testing.',
	}] for doctype in ('Sales Invoice', 'Payment Entry')})
	config = frappe.get_single('QAS Stripe Settings')
	if not config.company or not frappe.db.exists('Company', config.company):
		return
	name = frappe.db.get_value('Account', {'company': config.company, 'account_name': 'Stripe Test Clearing'}, 'name')
	if not name:
		parent = frappe.get_doc('Account', config.clearing_account).parent_account if config.clearing_account else bank_parent(config.company)
		name = frappe.get_doc({'doctype': 'Account', 'account_name': 'Stripe Test Clearing',
			'company': config.company, 'parent_account': parent, 'account_currency': 'AUD',
			'account_type': 'Bank', 'is_group': 0}).insert(ignore_permissions=True).name
	if not config.get('test_clearing_account'):
		frappe.db.set_single_value('QAS Stripe Settings', 'test_clearing_account', name)
