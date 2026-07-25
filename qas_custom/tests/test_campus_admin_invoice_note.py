from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from frappe import ValidationError

from qas_custom.api.campus_admin import campus_admin_convert_inquiry
from qas_custom.modules.workflows.trial_conversion import (
	apply_conversion_invoice_note,
	append_conversion_invoice_note,
	convert_inquiry_to_full_term_core,
	normalize_conversion_internal_note,
)
from qas_custom.modules.inquiry.notes import add_conversion_internal_note
from qas_custom.services.campus_admin import convert_campus_admin_inquiry_data


class TestCampusAdminInvoiceNote(TestCase):
	@patch("qas_custom.modules.inquiry.notes.add_system_note")
	def test_adds_non_empty_conversion_note_to_inquiry_timeline(self, add_system_note):
		inquiry = SimpleNamespace(name="INQ-001")
		invoice = SimpleNamespace(name="ACC-SINV-001")

		add_conversion_internal_note(
			inquiry,
			invoice,
			"Promised 10% discount",
			actor="campus@example.com",
		)

		add_system_note.assert_called_once_with(
			inquiry_doc=inquiry,
			note="Campus Admin conversion note: Promised 10% discount",
			source_doctype="Sales Invoice",
			source_document="ACC-SINV-001",
			actor="campus@example.com",
		)

	@patch("qas_custom.modules.inquiry.notes.add_system_note")
	def test_does_not_add_empty_conversion_note_to_inquiry_timeline(self, add_system_note):
		add_conversion_internal_note(SimpleNamespace(name="INQ-001"), SimpleNamespace(name="ACC-SINV-001"), "   ")

		add_system_note.assert_not_called()

	def test_normalizes_optional_note(self):
		self.assertEqual(normalize_conversion_internal_note(None), "")
		self.assertEqual(normalize_conversion_internal_note("   "), "")
		self.assertEqual(normalize_conversion_internal_note("  Promised 10% discount  "), "Promised 10% discount")

	def test_appends_note_without_overwriting_existing_remarks(self):
		self.assertEqual(
			append_conversion_invoice_note("Existing instruction", "Promised 10% discount"),
			"Existing instruction\nCampus Admin conversion note: Promised 10% discount",
		)
		self.assertEqual(
			append_conversion_invoice_note("", "Call before submitting"),
			"Campus Admin conversion note: Call before submitting",
		)

	def test_applies_note_to_invoice_and_saves_draft(self):
		invoice = Mock()
		invoice.get.return_value = "Existing instruction"
		with patch(
			"qas_custom.modules.workflows.trial_conversion.run_invoice_mutation_as_administrator",
			side_effect=lambda callback: callback(),
		) as run_mutation:
			self.assertIs(apply_conversion_invoice_note(invoice, "Promised 10% discount"), invoice)
		invoice.set.assert_called_once_with(
			"remarks",
			"Existing instruction\nCampus Admin conversion note: Promised 10% discount",
		)
		run_mutation.assert_called_once()
		invoice.save.assert_called_once_with(ignore_permissions=True)

	def test_empty_note_does_not_save_invoice(self):
		invoice = Mock()
		self.assertIs(apply_conversion_invoice_note(invoice, ""), invoice)
		invoice.set.assert_not_called()
		invoice.save.assert_not_called()

	@patch("qas_custom.modules.workflows.trial_conversion.frappe.throw", side_effect=ValidationError)
	def test_rejects_note_longer_than_1000_characters_before_conversion(self, _throw):
		with patch("qas_custom.modules.workflows.trial_conversion.get_inquiry_for_conversion") as get_inquiry:
			with self.assertRaises(ValidationError):
				convert_inquiry_to_full_term_core("INQ-001", "CS-001", internal_note="x" * 1001)
		get_inquiry.assert_not_called()

	@patch("qas_custom.api.campus_admin.convert_campus_admin_inquiry_data")
	def test_api_passes_internal_note(self, convert):
		convert.return_value = {"name": "INQ-001"}
		result = campus_admin_convert_inquiry.__wrapped__(
			inquiry="INQ-001",
			course_session="CS-001",
			internal_note="Promised 10% discount",
		)
		self.assertEqual(result, {"name": "INQ-001"})
		convert.assert_called_once_with(
			inquiry="INQ-001",
			course_session="CS-001",
			internal_note="Promised 10% discount",
		)

	@patch("qas_custom.services.campus_admin.convert_inquiry_to_full_term_core")
	@patch("qas_custom.services.campus_admin._validate_conversion_session_access")
	@patch("qas_custom.services.campus_admin._require_inquiry_access")
	@patch("qas_custom.services.campus_admin.reject_support_view_write")
	def test_service_passes_internal_note_to_shared_workflow(
		self,
		_reject_support,
		_require_access,
		_validate_access,
		convert,
	):
		convert.return_value = {"inquiry": {"name": "INQ-001"}}
		with patch(
			"qas_custom.services.campus_admin.frappe.session",
			SimpleNamespace(user="campus@example.com"),
		):
			result = convert_campus_admin_inquiry_data(
				inquiry="INQ-001",
				course_session="CS-001",
				internal_note="Promised 10% discount",
			)
		self.assertEqual(result, {"name": "INQ-001"})
		convert.assert_called_once_with(
			"INQ-001",
			"CS-001",
			actor="campus@example.com",
			internal_note="Promised 10% discount",
		)
