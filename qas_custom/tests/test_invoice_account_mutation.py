from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from qas_custom.modules.billing.commands import (
	create_prorata_invoice,
	run_invoice_mutation_as_administrator,
)


class TestInvoiceAccountMutation(TestCase):
	def test_mutation_runs_as_administrator_and_restores_original_user(self):
		session = SimpleNamespace(user="campus@example.com")
		observed_users = []

		def set_user(user):
			session.user = user

		with patch("qas_custom.modules.billing.commands.frappe.session", session), patch(
			"qas_custom.modules.billing.commands.frappe.set_user",
			side_effect=set_user,
		) as set_user_mock:
			result = run_invoice_mutation_as_administrator(lambda: observed_users.append(session.user) or "saved")

		self.assertEqual(result, "saved")
		self.assertEqual(observed_users, ["Administrator"])
		self.assertEqual(session.user, "campus@example.com")
		self.assertEqual(
			set_user_mock.call_args_list,
			[call("Administrator"), call("campus@example.com")],
		)

	def test_mutation_restores_original_user_after_error(self):
		session = SimpleNamespace(user="campus@example.com")

		def set_user(user):
			session.user = user

		def fail():
			raise RuntimeError("invoice validation failed")

		with patch("qas_custom.modules.billing.commands.frappe.session", session), patch(
			"qas_custom.modules.billing.commands.frappe.set_user",
			side_effect=set_user,
		):
			with self.assertRaisesRegex(RuntimeError, "invoice validation failed"):
				run_invoice_mutation_as_administrator(fail)

		self.assertEqual(session.user, "campus@example.com")

	def test_prorata_invoice_insert_and_update_use_protected_mutation(self):
		for is_new in (True, False):
			with self.subTest(is_new=is_new):
				invoice = Mock()
				invoice.append.return_value = Mock()
				inquiry = SimpleNamespace(name="INQ-001", parent="PARENT-001", student="STUDENT-001")
				enrollment = SimpleNamespace(name="ENROLL-001", weekly_timeslot="TS-001")
				with patch(
					"qas_custom.modules.billing.commands.get_prorata_invoice_context",
					return_value={"customer": "CUSTOMER-001", "item_code": "ITEM-001", "unit_rate": 50},
				), patch(
					"qas_custom.modules.billing.commands.get_or_create_course_invoice",
					return_value=invoice,
				), patch(
					"qas_custom.modules.billing.commands.is_new_doc",
					return_value=is_new,
				), patch(
					"qas_custom.modules.billing.commands.run_invoice_mutation_as_administrator",
					side_effect=lambda callback: callback(),
				) as run_mutation, patch(
					"qas_custom.modules.billing.commands.sync_invoice_student_summary"
				), patch(
					"qas_custom.modules.billing.commands.normalize_course_invoice_dates"
				), patch(
					"qas_custom.modules.billing.commands.apply_invoice_payment_snapshot"
				), patch(
					"qas_custom.modules.billing.commands.get_student_parent_name",
					return_value="Student Name",
				), patch(
					"qas_custom.modules.billing.commands.get_student_display_code",
					return_value="STUDENT-001",
				), patch(
					"qas_custom.modules.billing.commands.invoice_item_schedule",
					return_value="Tuesday 9:00 AM-10:30 AM",
				), patch(
					"qas_custom.modules.billing.commands.get_course_session_snapshot_label",
					return_value="28 Jul 2026 · 9:00 AM",
				):
					result = create_prorata_invoice(
						inquiry,
						enrollment,
						course="Anime Art",
						term="TERM-001",
						start_session="SESSION-001",
						remaining_session_count=5,
					)

				self.assertIs(result, invoice)
				run_mutation.assert_called_once()
				if is_new:
					invoice.insert.assert_called_once_with(ignore_permissions=True)
					invoice.save.assert_not_called()
				else:
					invoice.save.assert_called_once_with(ignore_permissions=True)
					invoice.insert.assert_not_called()
