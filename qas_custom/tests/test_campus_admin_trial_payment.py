from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.campus_admin import EFTPOS_MODE_OF_PAYMENT, _ensure_eftpos_mode_of_payment


class TestCampusAdminTrialPayment(TestCase):
	def test_eftpos_mode_is_created_from_cash_receiving_accounts(self):
		session = SimpleNamespace(user="campus@example.com")
		cash_mode = Mock()
		cash_mode.get.side_effect = lambda field, default=None: {
			"type": "Receive",
			"accounts": [
				{"company": "Queensland Art School", "default_account": "Cash - QAS"},
			],
		}.get(field, default)
		created_mode = Mock(name=EFTPOS_MODE_OF_PAYMENT)
		created_mode.name = EFTPOS_MODE_OF_PAYMENT
		fake_frappe = Mock()
		fake_frappe.session = session
		fake_frappe.db.exists.side_effect = [False, True, False]
		fake_frappe.get_doc.side_effect = [cash_mode, created_mode]

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			result = _ensure_eftpos_mode_of_payment()

		self.assertEqual(result, EFTPOS_MODE_OF_PAYMENT)
		fake_frappe.get_doc.assert_any_call("Mode of Payment", "Cash")
		create_payload = fake_frappe.get_doc.call_args_list[1].args[0]
		self.assertEqual(create_payload["mode_of_payment"], EFTPOS_MODE_OF_PAYMENT)
		self.assertEqual(create_payload["type"], "Receive")
		self.assertEqual(create_payload["accounts"], [{"company": "Queensland Art School", "default_account": "Cash - QAS"}])
		created_mode.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(fake_frappe.set_user.call_args_list[0].args, ("Administrator",))
		self.assertEqual(fake_frappe.set_user.call_args_list[1].args, ("campus@example.com",))

	def test_eftpos_mode_reuses_existing_configuration(self):
		fake_frappe = Mock()
		fake_frappe.db.exists.return_value = True

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			result = _ensure_eftpos_mode_of_payment()

		self.assertEqual(result, EFTPOS_MODE_OF_PAYMENT)
		fake_frappe.get_doc.assert_not_called()

	def test_eftpos_mode_requires_cash_configuration(self):
		fake_frappe = Mock()
		fake_frappe.db.exists.side_effect = [False, False]
		fake_frappe.throw.side_effect = RuntimeError("Cash is not configured")

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe), patch(
			"qas_custom.services.campus_admin._", side_effect=lambda value: value
		):
			with self.assertRaisesRegex(RuntimeError, "Cash is not configured"):
				_ensure_eftpos_mode_of_payment()

		fake_frappe.get_doc.assert_not_called()
