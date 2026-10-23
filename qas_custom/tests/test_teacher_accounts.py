from unittest import TestCase
from unittest.mock import MagicMock, patch
from types import ModuleType
import sys

import frappe

from qas_custom.services import teacher_accounts as accounts

require_account_admin = accounts._require_school_admin


class TestTeacherAccounts(TestCase):
	def setUp(self):
		self.admin = patch.object(accounts, "_require_school_admin")
		self.require_admin = self.admin.start()
		self.addCleanup(self.admin.stop)
		self.frappe_patch = patch.object(accounts, "frappe")
		self.frappe = self.frappe_patch.start()
		self.addCleanup(self.frappe_patch.stop)
		self.frappe.throw.side_effect = frappe.ValidationError
		self.frappe.db.exists.return_value = False
		self.translate = patch.object(accounts, "_", side_effect=lambda text: text)
		self.translate.start()
		self.addCleanup(self.translate.stop)
		self.email_validator = patch.object(accounts, "validate_email_address")
		self.validate_email = self.email_validator.start()
		self.addCleanup(self.email_validator.stop)
		self.user, self.teacher = MagicMock(), MagicMock()
		self.user.name = "teacher@example.com"
		self.teacher.name = "TEACHER-001"
		self.frappe.new_doc.side_effect = [self.user, self.teacher]

	def create(self, **overrides):
		return accounts.create_teacher_account_data(**{
			"teacher_name": "  New Teacher  ", "email": " Teacher@Example.COM ", "mobile": " 0400000000 ", **overrides,
		})

	def test_creates_linked_website_account_without_sending_mail_or_committing(self):
		result = self.create()
		self.require_admin.assert_called_once_with()
		self.validate_email.assert_called_once_with("teacher@example.com", throw=True)
		self.assertEqual(self.user.email, "teacher@example.com")
		self.assertEqual(self.user.first_name, "New Teacher")
		self.assertEqual(self.user.user_type, "Website User")
		self.assertEqual(self.user.enabled, 1)
		self.assertEqual(self.user.send_welcome_email, 0)
		self.assertEqual(self.teacher.user, self.user.name)
		self.assertEqual(self.teacher.status, "Active")
		self.assertEqual(self.teacher.mobile, "0400000000")
		self.assertEqual(result, {"name": "TEACHER-001", "teacher_name": "New Teacher", "email": "teacher@example.com", "mobile": "0400000000", "status": "Active", "user": "teacher@example.com"})
		self.user.insert.assert_called_once_with(ignore_permissions=True)
		self.teacher.insert.assert_called_once_with(ignore_permissions=True)
		self.frappe.sendmail.assert_not_called()
		self.frappe.db.commit.assert_not_called()
		self.frappe.db.rollback.assert_not_called()

	def test_unauthorized_request_cannot_read_or_write_accounts(self):
		self.require_admin.side_effect = frappe.PermissionError
		with self.assertRaises(frappe.PermissionError):
			self.create()
		self.frappe.db.exists.assert_not_called()
		self.frappe.new_doc.assert_not_called()

	def test_invalid_input_cannot_create_accounts(self):
		for fields in ({"teacher_name": " "}, {"email": ""}, {"email": "one@example.com,two@example.com"}, {"email": "Name <one@example.com>"}, {"email": "one @example.com"}, {"teacher_name": "a" * 141}, {"mobile": []}, {"email": 123}):
			with self.subTest(fields=fields), self.assertRaises(frappe.ValidationError):
				self.create(**fields)
		self.frappe.new_doc.assert_not_called()

	def test_invalid_email_is_rejected_by_frappe_validator(self):
		self.validate_email.side_effect = frappe.ValidationError
		with self.assertRaises(frappe.ValidationError):
			self.create(email="invalid")
		self.frappe.new_doc.assert_not_called()

	def test_duplicate_user_name_email_and_teacher_links_are_rejected(self):
		for match in range(4):
			with self.subTest(match=match):
				self.frappe.db.exists.side_effect = [False] * match + [True]
				with self.assertRaises(frappe.ValidationError):
					self.create()
		self.frappe.new_doc.assert_not_called()

	def test_teacher_failure_rolls_back_user_and_teacher(self):
		self.teacher.insert.side_effect = frappe.ValidationError("Duplicate teacher")
		with self.assertRaises(frappe.ValidationError):
			self.create()
		self.frappe.db.savepoint.assert_called_once_with("create_teacher_account")
		self.frappe.db.rollback.assert_called_once_with(save_point="create_teacher_account")
		self.frappe.db.commit.assert_not_called()

	def test_concurrent_duplicate_user_insert_rolls_back(self):
		self.user.insert.side_effect = frappe.DuplicateEntryError
		with self.assertRaises(frappe.DuplicateEntryError):
			self.create()
		self.frappe.db.rollback.assert_called_once_with(save_point="create_teacher_account")
		self.teacher.insert.assert_not_called()

	def test_unexpected_desk_access_is_rejected_and_rolled_back(self):
		self.user.insert.side_effect = lambda **kwargs: setattr(self.user, "user_type", "System User")
		with self.assertRaises(frappe.ValidationError):
			self.create()
		self.frappe.db.rollback.assert_called_once_with(save_point="create_teacher_account")
		self.teacher.insert.assert_not_called()

	def test_auth_guard_uses_existing_admin_and_support_view_checks(self):
		admin_module = ModuleType("qas_custom.services.school_admin")
		admin_module._require_school_admin = MagicMock()
		support_module = ModuleType("qas_custom.services.support_view")
		support_module.reject_support_view_write = MagicMock()
		with patch.dict(sys.modules, {admin_module.__name__: admin_module, support_module.__name__: support_module}):
			require_account_admin()
		support_module.reject_support_view_write.assert_called_once_with()
		admin_module._require_school_admin.assert_called_once_with()

	def test_support_view_is_rejected_before_admin_access_check(self):
		admin_module = ModuleType("qas_custom.services.school_admin")
		admin_module._require_school_admin = MagicMock()
		support_module = ModuleType("qas_custom.services.support_view")
		support_module.reject_support_view_write = MagicMock(side_effect=frappe.PermissionError)
		with patch.dict(sys.modules, {admin_module.__name__: admin_module, support_module.__name__: support_module}):
			with self.assertRaises(frappe.PermissionError):
				require_account_admin()
		admin_module._require_school_admin.assert_not_called()
