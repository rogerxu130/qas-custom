from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from qas_custom.patches import v2026_09_17_setup_stripe_accounts as setup


class StripeAccountSetupTests(TestCase):
	def setUp(self):
		self.config = frappe._dict(company='Queensland Art School', mode='Test', enabled=0)
		self.account = frappe._dict(name='Stripe Clearing - QAS', company=self.config.company,
			account_currency='AUD', root_type='Asset', account_type='Bank', is_group=0, disabled=0)
		self.mode = MagicMock()
		self.mode.enabled = 1
		self.mode.type = 'Bank'
		self.rows = []
		self.mode.get.side_effect = lambda field, default=None: self.rows if field == 'accounts' else default
		self.mode.append.side_effect = lambda field, row: self.rows.append(frappe._dict(row))
		self.mode.is_new.return_value = False
		self.db = MagicMock()
		self.db.exists.return_value = True
		self.db.set_single_value.side_effect = lambda dt, field, value: self.config.update({field: value})
		self.created = []
		self.account_exists = False
		def get_doc(dt, name=None):
			if isinstance(dt, dict):
				self.created.append(dt)
				if dt['doctype'] == 'Account':
					self.account_exists = True
					doc = MagicMock(name='account')
					doc.name = self.account.name
					return doc
				return self.mode
			return self.account if dt == 'Account' else self.mode
		def get_all(dt, filters, pluck):
			if filters.get('account_name') == 'Stripe Clearing':
				return [self.account.name] if self.account_exists else []
			return ['Bank Accounts - QAS']
		for name, value in [('db', self.db), ('get_single', MagicMock(return_value=self.config)),
			('get_doc', MagicMock(side_effect=get_doc)), ('get_all', MagicMock(side_effect=get_all)),
			('clear_document_cache', MagicMock()), ('throw', MagicMock(side_effect=ValueError))]:
			p = patch.object(setup.frappe, name, value)
			p.start()
			self.addCleanup(p.stop)

	def test_create_fill_and_rerun_without_duplicate_or_secret_changes(self):
		self.db.exists.side_effect = lambda dt, name: dt == 'Company' or bool(self.rows)
		setup.execute()
		setup.execute()
		self.assertEqual([doc['doctype'] for doc in self.created], ['Account', 'Mode of Payment'])
		self.assertEqual(len(self.rows), 1)
		self.assertEqual(self.config.clearing_account, self.account.name)
		self.assertEqual(self.config.mode_of_payment, 'Stripe')
		self.assertEqual(self.config.enabled, 0)
		self.assertEqual({call.args[1] for call in self.db.set_single_value.call_args_list},
			{'clearing_account', 'mode_of_payment'})

	def test_preserve_complete_live_config_and_existing_mapping(self):
		self.config.update(clearing_account=self.account.name, mode_of_payment='Custom Stripe', mode='Live', enabled=1)
		self.rows.append(frappe._dict(company=self.config.company, default_account='Existing Bank'))
		setup.execute()
		self.db.set_single_value.assert_not_called()
		self.mode.save.assert_not_called()
		self.assertEqual(self.rows[0].default_account, 'Existing Bank')

	def test_reuse_existing_account_and_add_missing_mapping(self):
		self.account_exists = True
		setup.execute()
		self.assertFalse(self.created)
		self.assertEqual(self.rows[0].default_account, self.account.name)

	def test_reject_invalid_account_without_changing_settings(self):
		self.account_exists = True
		self.account.account_currency = 'USD'
		with self.assertRaises(ValueError): setup.execute()
		self.db.set_single_value.assert_not_called()

	def test_reject_disabled_payment_mode(self):
		self.account_exists = True
		self.mode.enabled = 0
		with self.assertRaises(ValueError): setup.execute()
		self.db.set_single_value.assert_not_called()

	def test_missing_company_leaves_site_unchanged(self):
		self.db.exists.return_value = False
		setup.execute()
		self.assertFalse(self.created)
		self.db.set_single_value.assert_not_called()

	def test_bank_group_fallback_and_ambiguity(self):
		with patch.object(setup.frappe, 'get_all', side_effect=[[], ['Bank Accounts - QAS']]):
			self.assertEqual(setup.bank_parent(self.config.company), 'Bank Accounts - QAS')
		for candidates in ([], ['One', 'Two']):
			with patch.object(setup.frappe, 'get_all', return_value=candidates):
				with self.assertRaises(ValueError): setup.bank_parent(self.config.company)
