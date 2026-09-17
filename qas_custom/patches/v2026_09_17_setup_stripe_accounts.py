"""Initialize missing Stripe accounting references without changing payment access."""
import frappe
from frappe.utils import cint

SETTINGS = 'QAS Stripe Settings'


def bank_parent(company):
	groups = frappe.get_all('Account', filters={
		'company': company, 'root_type': 'Asset', 'is_group': 1,
		'disabled': 0, 'account_type': 'Bank',
	}, pluck='name')
	if not groups:
		groups = frappe.get_all('Account', filters={
			'company': company, 'root_type': 'Asset', 'is_group': 1,
			'disabled': 0, 'account_name': 'Bank Accounts',
		}, pluck='name')
	if len(groups) != 1:
		frappe.throw('Stripe setup needs one active Bank account group for ' + company
			+ '. Configure a clearing account in QAS Stripe Settings, then rerun migration.')
	return groups[0]


def execute():
	config = frappe.get_single(SETTINGS)
	company = config.company or 'Queensland Art School'
	# Other installations may not have the QAS company yet. Rerun after company setup.
	if not frappe.db.exists('Company', company):
		return

	account_name = config.clearing_account
	if not account_name:
		matches = frappe.get_all('Account', filters={
			'company': company, 'account_name': 'Stripe Clearing',
		}, pluck='name')
		if len(matches) > 1:
			frappe.throw('Multiple Stripe Clearing accounts exist. Select one in QAS Stripe Settings.')
		account_name = matches[0] if matches else None
		if not account_name:
			account = frappe.get_doc({
				'doctype': 'Account', 'account_name': 'Stripe Clearing',
				'company': company, 'parent_account': bank_parent(company),
				'account_currency': 'AUD', 'account_type': 'Bank', 'is_group': 0,
			})
			account.insert(ignore_permissions=True)
			account_name = account.name
	account = frappe.get_doc('Account', account_name)
	if (account.company != company or account.account_currency != 'AUD'
		or account.root_type != 'Asset' or cint(account.is_group) or cint(account.disabled)
		or account.account_type not in ('Bank', 'Cash')):
		frappe.throw('Stripe clearing account must be an active AUD Bank/Cash asset account for ' + company)

	mode_name = config.mode_of_payment or 'Stripe'
	if frappe.db.exists('Mode of Payment', mode_name):
		mode = frappe.get_doc('Mode of Payment', mode_name)
		if not cint(mode.enabled) or mode.type != 'Bank':
			frappe.throw('Stripe setup requires an enabled Bank mode of payment: ' + mode_name)
	else:
		mode = frappe.get_doc({'doctype': 'Mode of Payment',
			'mode_of_payment': mode_name, 'type': 'Bank', 'enabled': 1})
	rows = [row for row in mode.get('accounts', []) if row.company == company]
	changed = False
	if not rows:
		mode.append('accounts', {'company': company, 'default_account': account_name})
		changed = True
	# An existing company mapping belongs to the administrator; never overwrite it.
	if mode.is_new() or changed:
		mode.save(ignore_permissions=True)

	# Update only missing non-secret fields; avoid resaving masked Password fields.
	for field, value in {'company': company, 'clearing_account': account_name,
		'mode_of_payment': mode_name, 'mode': 'Test'}.items():
		if not config.get(field):
			frappe.db.set_single_value(SETTINGS, field, value)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
