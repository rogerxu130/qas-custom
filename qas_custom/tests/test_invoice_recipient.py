from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.modules.notifications.commands import _invoice_recipient


class TestInvoiceRecipient(TestCase):
	def resolve(self, invoice, students=None, guardians=None, parents=None, reverse=None):
		students = students or {"ENR-1": "STUDENT-1"}
		guardians = guardians or {"STUDENT-1": "PARENT-1"}
		parents = parents if parents is not None else {"PARENT-1": {"email": "new@example.com", "linked_user": "new@example.com"}}
		self.lookups = []

		def get_value(doctype, name, fields, **kwargs):
			self.lookups.append((doctype, name, fields))
			if doctype == "Enrollment":
				return students.get(name)
			if doctype == "Student":
				return guardians.get(name)
			if doctype == "Parent":
				return parents.get(name, {}) if isinstance(name, str) else None
			if doctype == "User":
				return name
			return None

		with patch("qas_custom.modules.notifications.commands.frappe") as mock, patch(
			"qas_custom.modules.notifications.commands._", side_effect=lambda text: text,
		):
			mock.db.exists.return_value = True
			mock.db.has_column.return_value = True
			mock.db.get_value.side_effect = get_value
			mock.get_all.return_value = reverse or []
			mock.throw.side_effect = frappe.ValidationError
			return _invoice_recipient(frappe._dict(invoice))

	def invoice(self, **kwargs):
		return {"name": "INV-1", "customer": "CUST-1", "contact_email": "old@example.com", **kwargs}

	def test_enrollment_source_uses_current_guardian_email(self):
		result = self.resolve(self.invoice(source_doctype="Enrollment", source_document="ENR-1"))
		self.assertEqual(result["email"], "new@example.com")
		self.assertEqual(result["parent"], "PARENT-1")
		self.assertEqual(result["for_user"], "new@example.com")

	def test_header_and_item_enrollment_links(self):
		for link in ({"enrollment": "ENR-1"}, {"items": [{"enrollment": "ENR-1"}]}):
			with self.subTest(link=link):
				self.assertEqual(self.resolve(self.invoice(**link))["email"], "new@example.com")

	def test_reverse_enrollment_invoice_link_without_items(self):
		self.assertEqual(self.resolve(self.invoice(), reverse=["ENR-1"])["email"], "new@example.com")

	def test_siblings_resolve_to_the_same_parent(self):
		result = self.resolve(self.invoice(items=[{"enrollment": "ENR-1"}, {"enrollment": "ENR-2"}]),
			students={"ENR-1": "STUDENT-1", "ENR-2": "STUDENT-2"},
			guardians={"STUDENT-1": "PARENT-1", "STUDENT-2": "PARENT-1"})
		self.assertEqual(result["email"], "new@example.com")

	def test_multiple_parents_block_sending(self):
		with self.assertRaises(frappe.ValidationError):
			self.resolve(self.invoice(items=[{"enrollment": "ENR-1"}, {"enrollment": "ENR-2"}]),
				students={"ENR-1": "STUDENT-1", "ENR-2": "STUDENT-2"},
				guardians={"STUDENT-1": "PARENT-1", "STUDENT-2": "PARENT-2"})

	def test_missing_current_email_does_not_use_old_invoice_email(self):
		result = self.resolve(self.invoice(enrollment="ENR-1"), parents={"PARENT-1": {}})
		self.assertIsNone(result["email"])

	def test_explicit_parent_still_has_priority(self):
		result = self.resolve(self.invoice(parent="PARENT-1", source_doctype="Enrollment", source_document="ENR-OTHER"))
		self.assertEqual(result["email"], "new@example.com")
		self.assertFalse(any(row[0] in ("Enrollment", "Student") for row in self.lookups))

	def test_unlinked_invoice_retains_legacy_email_fallback(self):
		self.assertEqual(self.resolve(self.invoice())["email"], "old@example.com")
