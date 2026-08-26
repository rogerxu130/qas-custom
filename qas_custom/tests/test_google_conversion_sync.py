import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.google_conversion_sync import (
	EXPECTED_HEADERS,
	GoogleSheetsClient,
	_capture_enabled,
	_event_payload,
	_extract_click_ids,
	_normalise_phone,
	_sheet_row,
	_submission_id,
	_trial_conversion_value,
	capture_inquiry_conversion_update,
	capture_payment_entry_submit,
)


class _InquiryDoc(dict):
	name = "INQ-001"

	def get(self, key, default=None):
		return super().get(key, default)

	def has_value_changed(self, fieldname):
		return fieldname in {"status", "converted_enrollment"}


class TestGoogleConversionSync(TestCase):
	def test_required_doctype_and_hooks_are_registered(self):
		self.assertTrue(frappe.db.exists("DocType", "Google Conversion Sync Event"))
		hooks = frappe.get_doc_hooks()
		self.assertIn(
			"qas_custom.services.google_conversion_sync.capture_payment_entry_submit",
			hooks["Payment Entry"]["on_submit"],
		)
		self.assertIn(
			"qas_custom.services.google_conversion_sync.capture_inquiry_conversion_update",
			hooks["Inquiry"]["on_update"],
		)

	def test_trial_conversion_value_uses_confirmed_threshold(self):
		self.assertEqual(_trial_conversion_value(55), 55)
		self.assertEqual(_trial_conversion_value(100), 100)
		self.assertEqual(_trial_conversion_value(100.01), 68)

	def test_click_ids_are_extracted_from_nested_submission(self):
		values = _extract_click_ids(
			{
				"utm": {"gclid": "CLICK-1"},
				"tracking": {"gbraid": "BRAID-1", "wbraid": "WBRAID-1"},
			}
		)
		self.assertEqual(values, {"gclid": "CLICK-1", "gbraid": "BRAID-1", "wbraid": "WBRAID-1"})

	def test_phone_is_normalised_for_australia(self):
		self.assertEqual(_normalise_phone("0412 345 678"), "+61412345678")
		self.assertEqual(_normalise_phone("+61 412 345 678"), "+61412345678")
		self.assertEqual(_normalise_phone("not supplied"), "not supplied")

	def test_submission_id_prefers_existing_then_derives_fluent_form_id(self):
		self.assertEqual(_submission_id({"external_submission_id": "fluent_form:5:237"}), "fluent_form:5:237")
		self.assertEqual(
			_submission_id({"external_form_id": "5", "external_serial_number": "237"}),
			"fluent_form:5:237",
		)

	def test_payload_maps_exact_sheet_columns(self):
		inquiry = frappe._dict(
			name="INQ-001",
			contact_email="PARENT@EXAMPLE.COM ",
			contact_phone="0412 345 678",
			external_submission_id="fluent_form:5:237",
			raw_webhook_payload=json.dumps({"gclid": "CLICK-1"}),
		)
		payload = _event_payload(
			inquiry,
			event_key="trial_invoice_paid:INV-001",
			event_name="QAS - Trial Invoice Paid",
			order_id="TRIAL-INQ-001",
			conversion_date_time="2026-08-26 14:32:18",
			conversion_value=68,
			source_doctype="Sales Invoice",
			source_document="INV-001",
			invoice="INV-001",
			payment_entry="PAY-001",
		)
		row = _sheet_row(payload)
		self.assertEqual(len(row), len(EXPECTED_HEADERS))
		self.assertEqual(row[0:5], ["QAS - Trial Invoice Paid", "2026-08-26 14:32:18", 68, "AUD", "TRIAL-INQ-001"])
		self.assertEqual(row[5], "CLICK-1")
		self.assertEqual(row[8], "parent@example.com")
		self.assertEqual(row[9], "+61412345678")
		self.assertEqual(row[11], "pending_review")

	@patch("qas_custom.services.google_conversion_sync.frappe.enqueue")
	@patch("qas_custom.services.google_conversion_sync._capture_enabled", return_value=True)
	def test_payment_entry_submit_enqueues_after_commit(self, _enabled, enqueue):
		doc = frappe._dict(
			name="PAY-001",
			docstatus=1,
			references=[
				frappe._dict(reference_doctype="Sales Invoice", reference_name="INV-001"),
				frappe._dict(reference_doctype="Sales Order", reference_name="SO-001"),
			],
		)
		capture_payment_entry_submit(doc)
		self.assertEqual(enqueue.call_args.kwargs["payment_entry"], "PAY-001")
		self.assertEqual(enqueue.call_args.kwargs["invoice_names"], ["INV-001"])
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("qas_custom.services.google_conversion_sync.frappe.enqueue")
	@patch("qas_custom.services.google_conversion_sync._capture_enabled", return_value=True)
	def test_converted_inquiry_enqueues_after_commit(self, _enabled, enqueue):
		doc = _InquiryDoc(
			inquiry_type="Trial Lesson",
			status="Converted",
			converted_enrollment="ENR-001",
		)
		capture_inquiry_conversion_update(doc)
		self.assertEqual(enqueue.call_args.kwargs["inquiry"], "INQ-001")
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_sheet_client_checks_existing_event_pair(self):
		client = GoogleSheetsClient.__new__(GoogleSheetsClient)
		client.get_values = Mock(
			return_value=[
				["QAS - Trial Invoice Paid", "2026-08-26 10:00:00", 68, "AUD", "TRIAL-INQ-001"],
			]
		)
		self.assertTrue(client.event_exists("QAS - Trial Invoice Paid", "TRIAL-INQ-001"))
		self.assertFalse(client.event_exists("QAS - Enrolled Student", "ENROL-INQ-001"))

	def test_sheet_client_appends_one_row(self):
		response = Mock()
		response.json.return_value = {"updates": {"updatedRange": "'Google Ads Upload'!A42:N42"}}
		response.raise_for_status.return_value = None
		client = GoogleSheetsClient.__new__(GoogleSheetsClient)
		client.session = Mock()
		client.session.post.return_value = response
		client.spreadsheet_id = "sheet-id"
		client.sheet_name = "Google Ads Upload"

		result = client.append_row(["value"] * len(EXPECTED_HEADERS))

		self.assertEqual(result, "'Google Ads Upload'!A42:N42")
		request = client.session.post.call_args
		self.assertEqual(request.kwargs["json"]["values"], [["value"] * len(EXPECTED_HEADERS)])
		self.assertEqual(request.kwargs["params"]["insertDataOption"], "INSERT_ROWS")

	@patch("qas_custom.services.google_conversion_sync.now_datetime", return_value=frappe.utils.get_datetime("2026-08-26 12:00:00"))
	@patch("qas_custom.services.google_conversion_sync._config_value")
	def test_capture_stays_enabled_when_google_credentials_are_not_configured(self, config_value, _now):
		config_value.side_effect = lambda environment_key, _conf_key: {
			"QAS_GOOGLE_CONVERSION_ENABLED": "1",
			"QAS_GOOGLE_CONVERSION_ENABLED_AT": "2026-08-26 11:00:00",
		}.get(environment_key)

		self.assertTrue(_capture_enabled())
