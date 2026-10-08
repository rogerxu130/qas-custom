from unittest import TestCase
from unittest.mock import patch

import frappe

from qas_custom.services.school_admin import _attach_invoice_display_names, get_school_admin_parents_data


class TestInvoiceDisplayNames(TestCase):
	def test_current_parent_name_and_ambiguous_customer_preserve_ids(self):
		parents = [frappe._dict(name="old-parent-id", parent_name="Maddie", customer="naddie")]
		customers = [frappe._dict(name="naddie", customer_name="Old customer name")]
		def get_all(doctype, **kwargs):
			return customers if doctype == "Customer" else parents
		with patch("qas_custom.services.school_admin._doctype_available", return_value=True), patch(
			"qas_custom.services.school_admin._has_field", return_value=True
		), patch("qas_custom.services.school_admin._safe_fields", side_effect=lambda _, fields: fields), patch(
			"qas_custom.services.school_admin.frappe.get_all", side_effect=get_all
		):
			rows = _attach_invoice_display_names([{"customer": "naddie"}, {"customer": "naddie", "parent": "old-parent-id"}])
			self.assertEqual([row["parent_name"] for row in rows], ["Maddie", "Maddie"])
			self.assertEqual(rows[0]["customer"], "naddie")
			self.assertEqual(rows[1]["parent"], "old-parent-id")
			parents.append(frappe._dict(name="another-parent", parent_name="Another parent", customer="naddie"))
			rows = _attach_invoice_display_names([{"customer": "naddie"}, {"customer": "naddie", "parent": "old-parent-id"}])
			self.assertEqual(rows[0]["parent_name"], "")
			self.assertEqual(rows[0]["customer_name"], "Old customer name")
			self.assertEqual(rows[1]["parent_name"], "Maddie")

	def test_family_search_includes_portal_login_email(self):
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._doctype_available", return_value=True
		), patch("qas_custom.services.school_admin._has_field", return_value=True), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _, fields: fields
		), patch("qas_custom.services.school_admin._matching_student_parent_ids", return_value=[]), patch(
			"qas_custom.services.school_admin.frappe.get_all", return_value=[]
		) as get_all:
			get_school_admin_parents_data(query="maddie@iresources.com.au")
			self.assertIn(["Parent", "linked_user", "like", "%maddie@iresources.com.au%"], get_all.call_args.kwargs["or_filters"])
