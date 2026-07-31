from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.campus_admin import (
	EFTPOS_MODE_OF_PAYMENT,
	_enqueue_campus_admin_trial_payment_notification,
	_ensure_eftpos_mode_of_payment,
	send_campus_admin_trial_payment_notification_job,
)


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

	def test_trial_payment_queues_school_admin_notification_after_commit(self):
		fake_frappe = Mock()
		inquiry = {"name": "INQ-001", "campus": "Indooroopilly", "submitted_student_name": "Student", "contact_name": "Parent"}

		with patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			result = _enqueue_campus_admin_trial_payment_notification(
				invoice="ACC-SINV-001", payment_entry="ACC-PAY-001", inquiry=inquiry, amount=68,
				payment_method="EFTPOS", note="Front desk", campus_admin="campus@example.com",
			)

		self.assertTrue(result["queued"])
		self.assertEqual(result["job_id"], "qas-campus-trial-payment-ACC-PAY-001")
		self.assertEqual(fake_frappe.enqueue.call_args.kwargs["invoice"], "ACC-SINV-001")
		self.assertEqual(fake_frappe.enqueue.call_args.kwargs["payment_entry"], "ACC-PAY-001")
		self.assertTrue(fake_frappe.enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("qas_custom.services.maintenance._get_school_admin_emails", return_value=["admin@example.com"])
	@patch("qas_custom.services.campus_admin.sendmail_or_skip")
	@patch("qas_custom.services.campus_admin.get_invoice_settings", return_value={"school_name": "Queensland Art School"})
	@patch("qas_custom.services.campus_admin._", side_effect=lambda value: value)
	def test_trial_payment_notification_email_identifies_the_payment(self, _translate, _settings, sendmail, _admins):
		sendmail.return_value = None
		result = send_campus_admin_trial_payment_notification_job(
			invoice="ACC-SINV-001", payment_entry="ACC-PAY-001", inquiry="INQ-001", campus="Indooroopilly",
			student="Student", parent="Parent", amount=68, payment_method="EFTPOS", note="Front desk", campus_admin="campus@example.com",
		)

		self.assertTrue(result["sent"])
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["admin@example.com"])
		self.assertIn("ACC-SINV-001", sendmail.call_args.kwargs["message"])
		self.assertIn("EFTPOS", sendmail.call_args.kwargs["message"])
