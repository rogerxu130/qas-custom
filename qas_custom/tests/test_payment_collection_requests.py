from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.payment_collection_requests import (
	_matching_parent_ids,
	_request_list,
	_request_payload,
	create_campus_payment_request_data,
	get_school_admin_payment_requests_data,
	get_invoice_payment_request_summaries,
	resolve_school_admin_payment_request_data,
	send_payment_collection_request_notification_job,
)


class TestPaymentCollectionRequests(TestCase):
	def _new_request_doc(self):
		doc = frappe._dict(name="PCR-2026-00001")
		doc.insert = Mock()
		return doc

	def _request_doc_factory(self, payload):
		doc = frappe._dict(payload)
		doc.name = "PCR-2026-00001"
		doc.insert = Mock()
		return doc

	def _submitted_request(self):
		return frappe._dict(
			name="PCR-2026-00001", request_type="Store Credit Top-up", status="Pending Review",
			campus="Campus A", parent="PAR-1", customer="CUS-1", invoice=None, collected_amount=1500,
			received_at="2026-07-18 14:30:00", payment_method="EFTPOS", reference_no="",
			campus_admin_note="", submitted_by="cashier@example.com", submitted_at="2026-07-18 14:31:00",
			invoice_outstanding_snapshot=0, notification_status="Sent", notification_error="",
			reviewed_by=None, reviewed_at=None, resolution_note="", payment_entry_reference=None,
			store_credit_reference=None,
		)

	@patch("qas_custom.services.payment_collection_requests._students_by_parent", return_value={})
	def test_school_admin_payload_includes_live_parent_identity(self, _students):
		db = SimpleNamespace(
			has_column=Mock(return_value=True),
			get_value=Mock(return_value=frappe._dict(
				name="PAR-1", parent_name="Roger Parent", email="parent@example.com",
				mobile_number="0400000000", customer="CUS-1",
			)),
		)
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db):
			result = _request_payload(self._submitted_request(), include_school_admin_identity=True)

		self.assertEqual(result["parent_email"], "parent@example.com")
		self.assertEqual(result["parent_phone"], "0400000000")
		self.assertEqual(result["customer"], "CUS-1")

	@patch("qas_custom.services.payment_collection_requests._students_by_parent", return_value={})
	def test_campus_admin_payload_does_not_include_school_admin_identity(self, _students):
		db = SimpleNamespace(get_value=Mock(return_value=frappe._dict(name="PAR-1", parent_name="Roger Parent")))
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db):
			result = _request_payload(self._submitted_request())

		self.assertNotIn("parent_email", result)
		self.assertNotIn("parent_phone", result)

	@patch("qas_custom.services.payment_collection_requests._matching_parent_ids", return_value=set())
	@patch("qas_custom.services.payment_collection_requests._doctype_available", return_value=True)
	@patch("qas_custom.services.payment_collection_requests.frappe.get_all", return_value=[])
	def test_request_search_matches_customer_id(self, get_all, _doctype, _matching_parents):
		db = SimpleNamespace(count=Mock(return_value=0))
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db):
			_request_list(query="CUS-1")

		self.assertIn(
			["Payment Collection Request", "customer", "like", "%CUS-1%"],
			get_all.call_args.kwargs["or_filters"],
		)

	@patch("qas_custom.services.payment_collection_requests.frappe.get_all")
	def test_parent_identity_search_includes_email_and_phone(self, get_all):
		get_all.side_effect = [["PAR-1"], []]
		db = SimpleNamespace(exists=Mock(return_value=True), has_column=Mock(return_value=True))
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db):
			result = _matching_parent_ids("parent@example.com")

		parent_filters = get_all.call_args_list[0].kwargs["or_filters"]
		self.assertIn(["Parent", "email", "like", "%parent@example.com%"], parent_filters)
		self.assertIn(["Parent", "mobile_number", "like", "%parent@example.com%"], parent_filters)
		self.assertEqual(result, {"PAR-1"})

	@patch("qas_custom.services.payment_collection_requests._request_list", return_value={"items": [], "pending_count": 0})
	@patch("qas_custom.services.payment_collection_requests._require_school_admin")
	def test_school_admin_list_requests_identity_fields(self, _require, request_list):
		get_school_admin_payment_requests_data(query="parent@example.com")

		self.assertTrue(request_list.call_args.kwargs["include_school_admin_identity"])

	@patch("qas_custom.services.payment_collection_requests._doctype_available", return_value=True)
	@patch("qas_custom.services.payment_collection_requests.frappe.get_all")
	def test_invoice_pending_summaries_are_loaded_in_one_query(self, mock_get_all, _doctype):
		mock_get_all.return_value = [frappe._dict(invoice="SINV-1"), frappe._dict(invoice="SINV-1")]

		result = get_invoice_payment_request_summaries(["SINV-1", "SINV-2"])

		mock_get_all.assert_called_once()
		self.assertEqual(result["SINV-1"]["pending_payment_request_count"], 2)
		self.assertEqual(result["SINV-2"]["pending_payment_request_count"], 0)

	@patch("qas_custom.services.payment_collection_requests._request_payload", side_effect=lambda doc, **_kwargs: dict(doc))
	@patch("qas_custom.services.payment_collection_requests.now_datetime", return_value="2026-07-17 10:00:00")
	@patch("qas_custom.services.payment_collection_requests._invoice_payable", return_value=500)
	@patch("qas_custom.services.payment_collection_requests._validate_invoice_for_parent")
	@patch("qas_custom.services.payment_collection_requests._campus_parent_ids", return_value={"PAR-1"})
	@patch("qas_custom.services.payment_collection_requests._doctype_available", return_value=True)
	@patch("qas_custom.services.payment_collection_requests._campus_scope", return_value=({"name": "CAP-1"}, ["Campus A"]))
	@patch("qas_custom.services.payment_collection_requests.reject_support_view_write")
	@patch("qas_custom.services.payment_collection_requests.frappe.get_doc")
	@patch("qas_custom.services.payment_collection_requests.frappe.enqueue")
	def test_over_collection_creates_review_request_without_financial_mutation(
		self, mock_enqueue, mock_get_doc, _reject, _scope, _doctype, _parents, mock_validate_invoice, _payable, _now, _payload
	):
		request_doc = self._request_doc_factory({})
		mock_get_doc.side_effect = lambda payload: request_doc.update(payload) or request_doc
		mock_validate_invoice.return_value = frappe._dict(name="SINV-1", docstatus=1, outstanding_amount=500)
		db = SimpleNamespace(
			get_value=Mock(side_effect=[None, frappe._dict(name="PAR-1", customer="CUS-1")]),
			commit=Mock(),
		)
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db), patch(
			"qas_custom.services.payment_collection_requests.frappe.session", SimpleNamespace(user="cashier@example.com")
		):
			result = create_campus_payment_request_data({
				"request_type": "Invoice Payment", "campus": "Campus A", "parent": "PAR-1", "invoice": "SINV-1",
				"collected_amount": 2000, "payment_method": "EFTPOS", "idempotency_key": "key-1",
			})

		self.assertEqual(request_doc.collected_amount, 2000)
		self.assertEqual(request_doc.invoice_outstanding_snapshot, 500)
		self.assertEqual(request_doc.status, "Pending Review")
		request_doc.insert.assert_called_once_with(ignore_permissions=True)
		mock_enqueue.assert_called_once()
		self.assertEqual(result["invoice"], "SINV-1")

	@patch("qas_custom.services.payment_collection_requests._request_payload", side_effect=lambda doc, **_kwargs: dict(doc))
	@patch("qas_custom.services.payment_collection_requests.now_datetime", return_value="2026-07-17 10:00:00")
	@patch("qas_custom.services.payment_collection_requests._validate_invoice_for_parent")
	@patch("qas_custom.services.payment_collection_requests._campus_parent_ids", return_value={"PAR-1"})
	@patch("qas_custom.services.payment_collection_requests._doctype_available", return_value=True)
	@patch("qas_custom.services.payment_collection_requests._campus_scope", return_value=({"name": "CAP-1"}, ["Campus A"]))
	@patch("qas_custom.services.payment_collection_requests.reject_support_view_write")
	@patch("qas_custom.services.payment_collection_requests.frappe.get_doc")
	@patch("qas_custom.services.payment_collection_requests.frappe.enqueue")
	def test_store_credit_top_up_does_not_require_or_validate_invoice(
		self, _enqueue, mock_get_doc, _reject, _scope, _doctype, _parents, mock_validate_invoice, _now, _payload
	):
		request_doc = self._request_doc_factory({})
		mock_get_doc.side_effect = lambda payload: request_doc.update(payload) or request_doc
		db = SimpleNamespace(
			get_value=Mock(side_effect=[None, frappe._dict(name="PAR-1", customer="CUS-1")]),
			commit=Mock(),
		)
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db), patch(
			"qas_custom.services.payment_collection_requests.frappe.session", SimpleNamespace(user="cashier@example.com")
		):
			create_campus_payment_request_data({
				"request_type": "Store Credit Top-up", "campus": "Campus A", "parent": "PAR-1",
				"collected_amount": 2000, "payment_method": "Cash", "idempotency_key": "key-2",
			})

		self.assertIsNone(request_doc.invoice)
		self.assertEqual(request_doc.invoice_outstanding_snapshot, 0)
		mock_validate_invoice.assert_not_called()

	@patch("qas_custom.services.payment_collection_requests._request_payload", side_effect=lambda doc, **_kwargs: dict(doc))
	@patch("qas_custom.services.payment_collection_requests.now_datetime", return_value="2026-07-17 11:00:00")
	@patch("qas_custom.services.payment_collection_requests._require_school_admin")
	@patch("qas_custom.services.payment_collection_requests.frappe.get_doc")
	def test_school_admin_resolution_only_updates_request(self, mock_get_doc, _require, _now, _payload):
		doc = frappe._dict(name="PCR-1", status="Pending Review")
		doc.save = Mock()
		mock_get_doc.return_value = doc
		db = SimpleNamespace(exists=Mock(return_value=True), commit=Mock())
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db), patch(
			"qas_custom.services.payment_collection_requests.frappe.session", SimpleNamespace(user="admin@example.com")
		):
			result = resolve_school_admin_payment_request_data("PCR-1", "Processed", "Recorded manually")

		self.assertEqual(doc.status, "Processed")
		self.assertEqual(doc.resolution_note, "Recorded manually")
		doc.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(result["status"], "Processed")

	@patch("qas_custom.services.maintenance._get_school_admin_emails", return_value=["admin@example.com"])
	@patch("qas_custom.services.payment_collection_requests.sendmail_or_skip", side_effect=RuntimeError("mail unavailable"))
	@patch("qas_custom.services.payment_collection_requests.frappe.get_doc")
	def test_notification_failure_keeps_request_and_records_failure(self, mock_get_doc, _sendmail, _recipients):
		mock_get_doc.return_value = frappe._dict(
			name="PCR-1", notification_status="Queued", campus="Campus A", collected_amount=2000,
			parent="PAR-1", request_type="Store Credit Top-up", invoice=None, submitted_by="cashier@example.com",
			payment_method="Cash", received_at="2026-07-17 10:00:00", reference_no="", campus_admin_note="",
		)
		db = SimpleNamespace(exists=Mock(return_value=True), set_value=Mock(), commit=Mock())
		with patch("qas_custom.services.payment_collection_requests.frappe.db", db), patch(
			"qas_custom.services.payment_collection_requests.frappe.log_error"
		), patch("qas_custom.services.payment_collection_requests.frappe.get_traceback", return_value="traceback"), patch(
			"qas_custom.services.payment_collection_requests._", side_effect=lambda value: value
		):
			result = send_payment_collection_request_notification_job("PCR-1")

		self.assertFalse(result["sent"])
		updates = db.set_value.call_args.args[2]
		self.assertEqual(updates["notification_status"], "Failed")
		self.assertIn("mail unavailable", updates["notification_error"])
