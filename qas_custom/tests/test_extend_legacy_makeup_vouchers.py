from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.patches import v2026_08_01_extend_legacy_makeup_vouchers as voucher_patch


class TestExtendLegacyMakeupVouchers(TestCase):
	def test_extends_only_currently_valid_legacy_ninety_day_vouchers(self):
		legacy = frappe._dict(name="MV-LEGACY", issue_date="2026-08-01", expiry_date="2026-10-30")
		custom = frappe._dict(name="MV-CUSTOM", issue_date="2026-08-01", expiry_date="2026-12-31")
		missing_issue_date = frappe._dict(name="MV-MISSING", issue_date=None, expiry_date="2026-10-30")
		fake_db = SimpleNamespace(
			exists=Mock(return_value=True),
			set_value=Mock(),
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			get_all=Mock(return_value=[legacy, custom, missing_issue_date]),
			clear_cache=Mock(),
		)

		with patch.object(voucher_patch, "frappe", fake_frappe), patch.object(
			voucher_patch, "today", return_value="2026-08-01"
		):
			voucher_patch.execute()

		self.assertEqual(fake_frappe.get_all.call_args.args, ("Makeup Voucher",))
		query = fake_frappe.get_all.call_args.kwargs
		self.assertEqual(query["filters"]["status"], "Valid")
		self.assertEqual(str(query["filters"]["expiry_date"][1]), "2026-08-01")
		self.assertEqual(query["fields"], ["name", "issue_date", "expiry_date"])
		self.assertEqual(query["limit_page_length"], 0)
		fake_db.set_value.assert_called_once()
		set_value_call = fake_db.set_value.call_args
		self.assertEqual(
			set_value_call.args[:3],
			("Makeup Voucher", "MV-LEGACY", "expiry_date"),
		)
		self.assertEqual(str(set_value_call.args[3]), "2027-08-01")
		self.assertEqual(set_value_call.kwargs, {"update_modified": False})
		fake_frappe.clear_cache.assert_called_once_with(doctype="Makeup Voucher")
