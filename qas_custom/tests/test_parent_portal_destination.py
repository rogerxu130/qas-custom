"""Portal routing is based on the authenticated family's historical records."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.api import parent_portal
from qas_custom.services import parent_portal_destination as destination


class TestParentPortalDestination(TestCase):
	def setUp(self):
		frappe.local.flags = frappe._dict(in_test=False)
		self.records = set()
		self.exists = Mock(side_effect=lambda doctype, filters: doctype in self.records)
		self.students = ["STU-1"]
		for setting in (
			patch.object(destination, "require_parent", return_value=SimpleNamespace(name="P-1")),
			patch.object(destination.frappe, "db", SimpleNamespace(exists=self.exists)),
			patch.object(destination.frappe, "get_all", side_effect=lambda *a, **kw: self.students),
		):
			setting.start()
			self.addCleanup(setting.stop)

	def test_payg_card_history_routes_to_psugo_even_without_standard_data(self):
		self.records.add("QAS PAYG Card")
		self.assertEqual(destination.get_parent_portal_context_data(), {
			"has_payg_access": True, "has_standard_access": False, "default_portal": "psugo",
		})
		self.exists.assert_any_call("QAS PAYG Card", {"family_parent": "P-1"})

	def test_purchase_operation_before_card_routes_to_psugo(self):
		self.records.add("QAS PAYG Operation")
		self.assertTrue(destination.get_parent_portal_context_data()["has_payg_access"])
		self.exists.assert_any_call("QAS PAYG Operation", {"family_parent": "P-1"})

	def test_standard_and_mixed_family(self):
		self.records.add("Enrollment")
		standard = destination.get_parent_portal_context_data()
		self.assertEqual(standard, {
			"has_payg_access": False, "has_standard_access": True, "default_portal": "parent",
		})
		self.exists.assert_any_call("Enrollment", {
			"parent": "P-1", "enrollment_type": ["!=", "Pay-as-you-go"],
		})
		self.records.add("QAS PAYG Card")
		self.assertEqual(destination.get_parent_portal_context_data()["default_portal"], "psugo")
		self.assertTrue(destination.get_parent_portal_context_data()["has_standard_access"])

	def test_legacy_enrollment_linked_only_by_student_is_standard(self):
		self.exists.side_effect = lambda doctype, filters: doctype == "Enrollment" and "student" in filters
		self.assertTrue(destination.get_parent_portal_context_data()["has_standard_access"])
		self.exists.assert_any_call("Enrollment", {
			"student": ["in", ["STU-1"]], "enrollment_type": ["!=", "Pay-as-you-go"],
		})

	def test_ordinary_invoice_is_standard_history(self):
		with patch.object(destination, "require_parent", return_value=SimpleNamespace(name="P-1", customer="C-1")):
			self.records.add("Sales Invoice")
			self.assertTrue(destination.get_parent_portal_context_data()["has_standard_access"])
		self.exists.assert_any_call("Sales Invoice", {
			"customer": "C-1", "docstatus": 1,
			"qas_invoice_type": ["in", ["Course", "Other", "Workshop"]],
		})

	def test_adhoc_workshop_and_store_are_standard_history(self):
		for doctype in ("Adhoc Booking", "Workshop Enrollment", "Store Order"):
			with self.subTest(doctype=doctype):
				self.records = {doctype}
				self.assertTrue(destination.get_parent_portal_context_data()["has_standard_access"])

	def test_family_without_history_defaults_to_parent(self):
		self.students = []
		self.assertEqual(destination.get_parent_portal_context_data(), {
			"has_payg_access": False, "has_standard_access": False, "default_portal": "parent",
		})

	def test_parent_resolution_failure_is_not_suppressed(self):
		with patch.object(destination, "require_parent", side_effect=PermissionError("no parent")):
			with self.assertRaises(PermissionError):
				destination.get_parent_portal_context_data()

	def test_api_is_thin_and_takes_no_family_parameter(self):
		with patch.object(parent_portal, "get_parent_portal_context_data", return_value={"default_portal": "parent"}) as read:
			self.assertEqual(parent_portal.parent_portal_get_portal_context(), {"default_portal": "parent"})
			read.assert_called_once_with()
