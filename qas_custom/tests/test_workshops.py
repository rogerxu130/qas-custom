from datetime import time, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.workshops import (
	ATTENDANCE_STATUSES,
	_build_session_detail,
	_copy_child_row_values,
	_find_draft_workshop_invoice,
	_invoice_has_workshop_enrollment_item,
	_relink_invoice_records,
	_validate_invoice_consolidation,
	_validate_no_invoice_payment_activity,
	_workshop_invoice_summary_map,
	activate_school_admin_workshop_enrollment_data,
	consolidate_school_admin_invoices_data,
	duplicate_school_admin_workshop_offering_data,
	_enrollment_payload,
	_serialise_workshop_time,
	_workshop_invoice_item,
	_update_attendance,
)
from qas_custom.services.school_admin import _get_family_workshop_enrollment_rows


class TestWorkshops(TestCase):
	@patch("qas_custom.services.workshops.get_invoice_payable_amount", side_effect=[0, 25])
	@patch("qas_custom.services.workshops.frappe")
	def test_workshop_invoice_summary_uses_live_document_and_payable_state(self, workshop_frappe, _payable):
		workshop_frappe.get_all.return_value = [
			frappe._dict(name="SINV-DRAFT", docstatus=0, status="Draft", outstanding_amount=200),
			frappe._dict(name="SINV-CANCELLED", docstatus=2, status="Cancelled", outstanding_amount=200),
			frappe._dict(name="SINV-PAID", docstatus=1, status="Paid", outstanding_amount=0),
			frappe._dict(name="SINV-OUT", docstatus=1, status="Overdue", outstanding_amount=25),
		]

		result = _workshop_invoice_summary_map(["SINV-DRAFT", "SINV-CANCELLED", "SINV-PAID", "SINV-OUT"])

		self.assertEqual(result["SINV-DRAFT"], {"status": "Draft", "outstanding_amount": 0})
		self.assertEqual(result["SINV-CANCELLED"], {"status": "Cancelled", "outstanding_amount": 0})
		self.assertEqual(result["SINV-PAID"], {"status": "Paid", "outstanding_amount": 0})
		self.assertEqual(result["SINV-OUT"], {"status": "Outstanding", "outstanding_amount": 25})

	@patch("qas_custom.services.workshops._content_for_sessions", return_value={"homeworks": [], "photo_posts": [], "video_posts": []})
	@patch("qas_custom.services.school_admin._get_family_outstanding_invoice_map", return_value={"PAR-1": {"amount": 125}})
	@patch("qas_custom.services.workshops.get_invoice_payable_amount", return_value=75)
	@patch("qas_custom.services.workshops.has_field", return_value=True)
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	def test_admin_workshop_session_includes_account_and_live_invoice_status(
		self, workshop_frappe, required_doc, _has_field, _payable, family_outstanding, _content
	):
		session = frappe._dict(
			name="WSS-1", workshop_offering="WSO-1", session_date="2026-09-22",
			start_time="09:00:00", end_time="16:00:00", workshop_session_index=1,
			workshop_session_count=3, status="Scheduled",
		)
		required_doc.return_value = frappe._dict(
			name="WSO-1", title="Spring Camp", workshop_category="Holiday Camp", class_language="English"
		)
		workshop_frappe.get_all.side_effect = [
			[frappe._dict(name="WAT-1", workshop_enrollment="WEN-1", student="STU-1", status="Not Marked", comments="")],
			[frappe._dict(name="STU-1", student_name="Amy", guardian="PAR-LEGACY", teaching_notes="")],
			[frappe._dict(name="WEN-1", parent="PAR-1", invoice="SINV-1")],
			[frappe._dict(name="PAR-1", parent_name="Parent One", mobile_number="0400", linked_user="p@example.com", customer="CUS-1")],
			[frappe._dict(name="SINV-1", docstatus=1, status="Overdue", grand_total=200, rounded_total=200, outstanding_amount=75)],
		]

		result = _build_session_detail(session, audience="admin")

		row = result["students"][0]
		self.assertTrue(row["has_outstanding_invoice"])
		self.assertEqual(row["outstanding_amount"], 125)
		self.assertEqual(row["workshop_invoice"], "SINV-1")
		self.assertEqual(row["workshop_invoice_status"], "Outstanding")
		self.assertEqual(row["workshop_invoice_outstanding_amount"], 75)
		self.assertEqual(row["parent_name"], "Parent One")
		family_outstanding.assert_called_once_with([{"parent": "PAR-1", "customer": "CUS-1"}])

	@patch("qas_custom.services.workshops._content_for_sessions", return_value={"homeworks": [], "photo_posts": [], "video_posts": []})
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	def test_teacher_workshop_session_omits_financial_fields(self, workshop_frappe, required_doc, _content):
		session = frappe._dict(
			name="WSS-1", workshop_offering="WSO-1", session_date="2026-09-22",
			start_time="09:00:00", end_time="16:00:00", workshop_session_index=1,
			workshop_session_count=3, status="Scheduled",
		)
		required_doc.return_value = frappe._dict(
			name="WSO-1", title="Spring Camp", workshop_category="Holiday Camp", class_language="English"
		)
		workshop_frappe.get_all.side_effect = [
			[frappe._dict(name="WAT-1", workshop_enrollment="WEN-1", student="STU-1", status="Not Marked", comments="")],
			[frappe._dict(name="STU-1", student_name="Amy", guardian="PAR-1", teaching_notes="")],
			[frappe._dict(name="PAR-1", parent_name="Parent One", mobile_number="0400", linked_user="p@example.com")],
		]

		row = _build_session_detail(session, audience="teacher")["students"][0]

		for field in (
			"has_outstanding_invoice", "outstanding_amount", "workshop_invoice",
			"workshop_invoice_status", "workshop_invoice_outstanding_amount",
		):
			self.assertNotIn(field, row)

	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe")
	def test_family_workshop_enrollments_include_all_statuses_and_display_context(self, school_admin_frappe, _doctype):
		school_admin_frappe.get_all.side_effect = [
			[
				frappe._dict(name="WEN-2", workshop_offering="WSO-1", student="STU-2", parent="PAR-1", status="Cancelled", standard_price_snapshot=180, invoice="SINV-2", invoice_status="Cancelled", invoice_amount=180, modified="2026-09-03 10:00:00"),
				frappe._dict(name="WEN-4", workshop_offering="WSO-1", student="STU-2", parent="PAR-1", status="Completed", standard_price_snapshot=180, modified="2026-09-03 09:00:00"),
				frappe._dict(name="WEN-3", workshop_offering="WSO-1", student="STU-1", parent="PAR-1", status="Planned", standard_price_snapshot=180, modified="2026-09-03 08:00:00"),
				frappe._dict(name="WEN-1", workshop_offering="WSO-1", student="STU-1", parent="PAR-1", status="Active", standard_price_snapshot=180, invoice="SINV-1", invoice_status="Draft", invoice_amount=180, modified="2026-09-02 10:00:00"),
			],
			[frappe._dict(name="WSO-1", title="Spring Painting", workshop_category="Holiday Camp", campus="Indooroopilly")],
			[
				frappe._dict(name="WSS-2", workshop_offering="WSO-1", session_date="2026-09-23", start_time="09:00:00"),
				frappe._dict(name="WSS-1", workshop_offering="WSO-1", session_date="2026-09-22", start_time="09:00:00"),
			],
		]

		result = _get_family_workshop_enrollment_rows(
			parent="PAR-1",
			students=["STU-1", "STU-2"],
			student_rows=[
				frappe._dict(name="STU-1", student_display="Amy (STU-1)"),
				frappe._dict(name="STU-2", student_name="Ben"),
			],
		)

		self.assertEqual([row["name"] for row in result], ["WEN-2", "WEN-4", "WEN-3", "WEN-1"])
		self.assertEqual({row["status"] for row in result}, {"Planned", "Active", "Completed", "Cancelled"})
		self.assertEqual(result[0]["student_display"], "Ben")
		self.assertEqual(result[3]["student_display"], "Amy (STU-1)")
		self.assertEqual(result[0]["workshop_title"], "Spring Painting")
		self.assertEqual(result[0]["session_dates"], ["2026-09-22", "2026-09-23"])
		first_call = school_admin_frappe.get_all.call_args_list[0]
		self.assertEqual(first_call.kwargs["filters"], {"parent": "PAR-1", "student": ["in", ["STU-1", "STU-2"]]})
		self.assertNotIn("status", first_call.kwargs["filters"])
		self.assertEqual(first_call.kwargs["order_by"], "modified desc, name desc")

	@patch("qas_custom.services.school_admin._doctype_available", return_value=False)
	@patch("qas_custom.services.school_admin.frappe")
	def test_family_workshop_enrollments_are_empty_during_staged_schema_deployment(self, school_admin_frappe, _doctype):
		result = _get_family_workshop_enrollment_rows(parent="PAR-1", students=["STU-1"])

		self.assertEqual(result, [])
		school_admin_frappe.get_all.assert_not_called()

	@patch("qas_custom.services.workshops.has_field", return_value=True)
	@patch("qas_custom.services.workshops.frappe")
	def test_find_draft_workshop_invoice_uses_family_customer_and_type(self, workshop_frappe, _has_field):
		workshop_frappe.get_all.return_value = ["ACC-SINV-0001"]

		result = _find_draft_workshop_invoice("PAR-1", "CUS-1")

		self.assertEqual(result, "ACC-SINV-0001")
		workshop_frappe.get_all.assert_called_once_with(
			"Sales Invoice",
			filters={
				"customer": "CUS-1",
				"docstatus": 0,
				"qas_invoice_type": "Workshop",
				"parent": "PAR-1",
				"status": ["!=", "Cancelled"],
			},
			pluck="name",
			order_by="creation asc",
			limit=1,
		)

	def test_invoice_has_workshop_enrollment_item_matches_line_type_and_enrollment(self):
		invoice = frappe._dict(items=[
			frappe._dict(qas_line_type="Course", enrollment="WEN-1"),
			frappe._dict(qas_line_type="Workshop", enrollment="WEN-2"),
		])

		self.assertTrue(_invoice_has_workshop_enrollment_item(invoice, "WEN-2"))
		self.assertFalse(_invoice_has_workshop_enrollment_item(invoice, "WEN-1"))

	def test_copy_child_row_values_removes_document_metadata(self):
		row = frappe._dict(name="ROW-1", parent="SINV-1", parenttype="Sales Invoice", item_code="Workshop Fee", rate=200)

		self.assertEqual(_copy_child_row_values(row), {"item_code": "Workshop Fee", "rate": 200})

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_consolidation_validation_rejects_duplicate_enrollment(self, _translate, workshop_frappe):
		workshop_frappe.throw.side_effect = frappe.ValidationError
		invoices = [
			frappe._dict(name="SINV-1", docstatus=0, status="Draft", qas_invoice_type="Workshop", parent="PAR-1", customer="CUS-1", company="QAS", currency="AUD", items=[frappe._dict(qas_line_type="Workshop", enrollment="WEN-1")], taxes=[]),
			frappe._dict(name="SINV-2", docstatus=0, status="Draft", qas_invoice_type="Workshop", parent="PAR-1", customer="CUS-1", company="QAS", currency="AUD", items=[frappe._dict(qas_line_type="Workshop", enrollment="WEN-1")], taxes=[]),
		]

		with self.assertRaises(frappe.ValidationError):
			_validate_invoice_consolidation(invoices)

		workshop_frappe.throw.assert_called_once_with("Enrollment WEN-1 appears on more than one selected invoice.")

	@patch("qas_custom.services.workshops.frappe")
	def test_consolidation_accepts_mixed_invoice_types_for_same_family(self, workshop_frappe):
		invoices = [
			frappe._dict(name="SINV-1", docstatus=0, status="Draft", qas_invoice_type="Course", parent="PAR-1", customer="CUS-1", company="QAS", currency="AUD", items=[], taxes=[]),
			frappe._dict(name="SINV-2", docstatus=0, status="Draft", qas_invoice_type="Workshop", parent="PAR-1", customer="CUS-1", company="QAS", currency="AUD", items=[], taxes=[]),
		]

		_validate_invoice_consolidation(invoices)

		workshop_frappe.throw.assert_not_called()

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_consolidation_rejects_different_currency(self, _translate, workshop_frappe):
		workshop_frappe.throw.side_effect = frappe.ValidationError
		invoices = [
			frappe._dict(name="SINV-1", docstatus=0, status="Draft", parent="PAR-1", customer="CUS-1", company="QAS", currency="AUD", items=[], taxes=[]),
			frappe._dict(name="SINV-2", docstatus=0, status="Draft", parent="PAR-1", customer="CUS-1", company="QAS", currency="USD", items=[], taxes=[]),
		]

		with self.assertRaises(frappe.ValidationError):
			_validate_invoice_consolidation(invoices)

		workshop_frappe.throw.assert_called_once_with("Selected invoices must use the same Currency.")

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_consolidation_rejects_incompatible_discounts(self, _translate, workshop_frappe):
		workshop_frappe.throw.side_effect = frappe.ValidationError
		base = {"docstatus": 0, "status": "Draft", "parent": "PAR-1", "customer": "CUS-1",
			"company": "QAS", "currency": "AUD", "items": [], "taxes": []}
		first = frappe._dict(**base, additional_discount_percentage=10)
		second = frappe._dict(**base, additional_discount_percentage=20)
		with self.assertRaises(frappe.ValidationError):
			_validate_invoice_consolidation([first, second])

	@patch("qas_custom.services.workshops._relink_invoice_records")
	@patch("qas_custom.services.workshops._invoice_linked_records", return_value={"Workshop Enrollment": ["WEN-1", "WEN-2"]})
	@patch("qas_custom.services.workshops._validate_no_invoice_payment_activity")
	@patch("qas_custom.services.workshops.apply_invoice_payment_snapshot")
	@patch("qas_custom.services.workshops.sync_invoice_student_summary")
	@patch("qas_custom.services.workshops.run_invoice_mutation_as_administrator", side_effect=lambda callback: callback())
	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops.frappe")
	def test_consolidate_mixed_drafts_keeps_oldest_and_relinks_sources(
		self, workshop_frappe, _require, _administrator, sync_summary, payment_snapshot, payment_activity, linked_records, relink_records
	):
		def invoice(name, creation, enrollment, amount, invoice_type):
			doc = Mock()
			doc.name = name
			doc.docstatus = 0
			doc.grand_total = amount
			doc.creation = creation
			doc.items = [frappe._dict(qas_line_type="Workshop", enrollment=enrollment, item_code="Workshop Fee", rate=amount)]
			doc.taxes = []
			values = {
				"creation": creation, "status": "Draft", "qas_invoice_type": invoice_type,
				"source_doctype": "Workshop Enrollment",
				"parent": "PAR-1", "customer": "CUS-1", "company": "QAS", "currency": "AUD",
				"items": doc.items, "taxes": doc.taxes,
			}
			doc.get.side_effect = lambda field, default=None: values.get(field, default)
			def append(field, values):
				row = frappe._dict(values)
				getattr(doc, field).append(row)
				return row
			doc.append.side_effect = append
			return doc

		target = invoice("SINV-OLD", "2026-09-01 09:00:00", "WEN-1", 200, "Workshop")
		source = invoice("SINV-NEW", "2026-09-02 09:00:00", "WEN-2", 180, "Course")
		workshop_frappe.get_doc.side_effect = lambda doctype, name, **_kwargs: {
			("Sales Invoice", "SINV-OLD"): target,
			("Sales Invoice", "SINV-NEW"): source,
		}[(doctype, name)]
		target.save.side_effect = lambda **_kwargs: setattr(target, "grand_total", 380)

		with patch("qas_custom.services.workshops.lock_payg_operations_for_invoices", return_value={}):
			result = consolidate_school_admin_invoices_data({"invoices": ["SINV-NEW", "SINV-OLD"]})

		self.assertEqual(result["invoice"], "SINV-OLD")
		self.assertEqual(result["merged_invoices"], 1)
		self.assertEqual(len(target.items), 2)
		sync_summary.assert_called_once_with(target)
		payment_snapshot.assert_called_once_with(target)
		target.set.assert_called_with("qas_invoice_type", "Other")
		linked_records.assert_called_once_with(["SINV-NEW", "SINV-OLD"])
		payment_activity.assert_called_once_with(["SINV-NEW", "SINV-OLD"])
		relink_records.assert_called_once_with(
			{"Workshop Enrollment": ["WEN-1", "WEN-2"]}, target, ["SINV-NEW", "SINV-OLD"]
		)
		workshop_frappe.delete_doc.assert_called_once_with("Sales Invoice", "SINV-NEW", ignore_permissions=True)
		workshop_frappe.db.commit.assert_called_once()

	@patch("qas_custom.services.workshops._relink_invoice_records")
	@patch("qas_custom.services.workshops._invoice_linked_records", return_value={})
	@patch("qas_custom.services.workshops._validate_no_invoice_payment_activity")
	@patch("qas_custom.services.workshops.apply_invoice_payment_snapshot")
	@patch("qas_custom.services.workshops.sync_invoice_student_summary")
	@patch("qas_custom.services.workshops.run_invoice_mutation_as_administrator", side_effect=lambda callback: callback())
	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops.frappe")
	def test_consolidate_two_payg_drafts_preserves_each_source_and_relinks_before_delete(
		self, workshop_frappe, _require, _administrator, _summary, _snapshot, _payment,
		_linked, _relink_legacy,
	):
		def invoice(name, creation, operation):
			doc = Mock()
			doc.name, doc.creation, doc.docstatus, doc.grand_total = name, creation, 0, 100
			doc.customer, doc.parent = "CUS-1", "PAR-1"
			doc.items = [frappe._dict(name="ROW-" + operation, item_code="PAYG",
				qas_source_doctype="QAS PAYG Operation", qas_source_document=operation)]
			doc.taxes = []
			values = {"creation": creation, "status": "Draft", "qas_invoice_type": "PAYG Card",
				"parent": doc.parent, "customer": doc.customer, "company": "QAS", "currency": "AUD",
				"items": doc.items, "taxes": doc.taxes}
			doc.get.side_effect = lambda key, default=None: values.get(key, default)
			def append(field, row_values):
				row = frappe._dict(row_values)
				getattr(doc, field).append(row)
				return row
			doc.append.side_effect = append
			def save(**_kwargs):
				for index, row in enumerate(doc.items):
					row.name = row.get("name") or f"MERGED-{index}"
			doc.save.side_effect = save
			return doc

		target = invoice("SINV-OLD", "2026-09-01", "OP-1")
		source = invoice("SINV-NEW", "2026-09-02", "OP-2")
		operations = {name: frappe._dict(name=name, invoice=invoice_name,
			family_parent="PAR-1", customer="CUS-1", invoice_request_key="key-" + name,
			save=Mock()) for name, invoice_name in (("OP-1", target.name), ("OP-2", source.name))}
		order = []
		def get_doc(doctype, name, **kwargs):
			if kwargs.get("for_update"):
				order.append("invoice-lock")
			return {target.name: target, source.name: source}[name]
		workshop_frappe.get_doc.side_effect = get_doc
		workshop_frappe.delete_doc.side_effect = lambda *_args, **_kwargs: self.assertEqual(operations["OP-2"].invoice, target.name)
		with patch("qas_custom.services.workshops.lock_payg_operations_for_invoices",
				side_effect=lambda _names: (order.append("operations-lock"), operations)[1]):
			result = consolidate_school_admin_invoices_data({"invoices": [source.name, target.name]})
		self.assertEqual(result["invoice"], target.name)
		self.assertEqual(order[:2], ["operations-lock", "invoice-lock"])
		self.assertEqual([row.qas_source_document for row in target.items], ["OP-1", "OP-2"])
		self.assertEqual(operations["OP-2"].invoice_request_key, "key-OP-2")
		operations["OP-2"].save.assert_called_once_with(ignore_permissions=True)

	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops.frappe")
	def test_support_view_blocks_payg_consolidation_before_invoice_mutation(self, workshop_frappe, _require):
		from qas_custom.modules.billing import payg_drafts
		workshop_frappe.db.savepoint = Mock()
		workshop_frappe.db.rollback = Mock()
		workshop_frappe.db.commit = Mock()
		with patch("qas_custom.services.workshops.lock_payg_operations_for_invoices",
				return_value={"OP-1": object()}), patch.object(
			payg_drafts, "get_support_view_token", return_value="support-token"
		), patch.object(
			payg_drafts.frappe, "throw", side_effect=lambda message, *_args: (_ for _ in ()).throw(PermissionError(message))
		), patch("qas_custom.services.workshops.relink_consolidated_payg_operations") as relink:
			with self.assertRaisesRegex(PermissionError, "Support View"):
				consolidate_school_admin_invoices_data({"invoices": ["SINV-1", "SINV-2"]})
		relink.assert_not_called()
		workshop_frappe.get_doc.assert_not_called()
		workshop_frappe.delete_doc.assert_not_called()
		workshop_frappe.db.commit.assert_not_called()
		workshop_frappe.db.rollback.assert_called_once()

	@patch("qas_custom.services.workshops.set_if_field")
	@patch("qas_custom.services.workshops.frappe")
	def test_relink_invoice_records_updates_supported_source_documents(self, workshop_frappe, set_if_field_mock):
		enrollment = Mock()
		workshop = Mock()
		store_order = Mock()
		inquiry = Mock()
		inquiry.get.side_effect = lambda field: {"trial_invoice": "SINV-2", "converted_invoice": "SINV-OTHER"}.get(field)
		workshop_frappe.get_doc.side_effect = lambda doctype, _name: {
			"Enrollment": enrollment,
			"Workshop Enrollment": workshop,
			"Store Order": store_order,
			"Inquiry": inquiry,
		}[doctype]
		target = frappe._dict(name="SINV-1", grand_total=380)

		_relink_invoice_records(
			{"Enrollment": ["ENR-1"], "Workshop Enrollment": ["WEN-1"], "Store Order": ["ORD-1"], "Inquiry": ["INQ-1"]},
			target,
			["SINV-1", "SINV-2"],
		)

		enrollment.set.assert_called_once_with("invoice", "SINV-1")
		workshop.set.assert_called_once_with("invoice", "SINV-1")
		store_order.set.assert_called_once_with("invoice", "SINV-1")
		inquiry.set.assert_called_once_with("trial_invoice", "SINV-1")
		self.assertNotIn("converted_invoice", [call.args[0] for call in inquiry.set.call_args_list])
		self.assertEqual(set_if_field_mock.call_count, 6)
		for doc in (enrollment, workshop, store_order, inquiry):
			doc.save.assert_called_once_with(ignore_permissions=True)

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_consolidation_rejects_active_payment_request(self, _translate, workshop_frappe):
		workshop_frappe.db.exists.side_effect = [True, "PAY-REQ-1"]
		workshop_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			_validate_no_invoice_payment_activity(["SINV-1", "SINV-2"])

		workshop_frappe.throw.assert_called_once_with(
			"Selected invoices have an active Payment Request and cannot be consolidated."
		)

	@patch("qas_custom.services.workshops.frappe")
	def test_workshop_invoice_item_prefers_configured_item(self, workshop_frappe):
		workshop_frappe.conf.get.side_effect = ["Configured Workshop Fee"]
		workshop_frappe.db.exists.return_value = True

		self.assertEqual(_workshop_invoice_item(), "Configured Workshop Fee")
		workshop_frappe.db.exists.assert_called_once_with("Item", "Configured Workshop Fee")

	@patch("qas_custom.services.workshops.frappe")
	def test_workshop_invoice_item_falls_back_to_existing_workshop_fee(self, workshop_frappe):
		workshop_frappe.conf.get.side_effect = [None, None]
		workshop_frappe.db.exists.return_value = True

		self.assertEqual(_workshop_invoice_item(), "Workshop Fee")
		workshop_frappe.db.exists.assert_called_once_with("Item", "Workshop Fee")

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_workshop_invoice_item_rejects_missing_configured_item(self, _translate, workshop_frappe):
		workshop_frappe.conf.get.side_effect = ["Missing Item"]
		workshop_frappe.db.exists.return_value = False
		workshop_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			_workshop_invoice_item()

		workshop_frappe.throw.assert_called_once_with("Configured Workshop invoice Item Missing Item does not exist.")

	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops.get_student_parent_name", return_value="Student One")
	def test_enrollment_payload_includes_student_and_parent_display_names(self, _student_name, workshop_frappe):
		workshop_frappe.db.get_value.return_value = "Parent One"

		payload = _enrollment_payload(frappe._dict(name="WEN-1", student="STU-1", parent="PAR-1"))

		self.assertEqual(payload["student_name"], "Student One")
		self.assertEqual(payload["parent_name"], "Parent One")
		workshop_frappe.db.get_value.assert_called_once_with("Parent", "PAR-1", "parent_name")

	def test_workshop_time_serialization_zero_pads_morning_hours(self):
		self.assertEqual(_serialise_workshop_time("9:30:00"), "09:30:00")
		self.assertEqual(_serialise_workshop_time(timedelta(hours=9, minutes=30)), "09:30:00")
		self.assertEqual(_serialise_workshop_time(time(16, 0)), "16:00:00")
		self.assertEqual(_serialise_workshop_time(None), "")

	@patch("qas_custom.services.workshops._build_offering_detail", return_value={"name": "WSO-COPY"})
	@patch("qas_custom.services.workshops._sync_session_positions")
	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_duplicate_copies_offering_and_sessions_only(
		self, _translate, workshop_frappe, required_doc, _require, sync_positions, build_detail
	):
		source = frappe._dict(
			name="WSO-ORIGINAL", title="Watercolour Day",
			workshop_category="General Workshop", participation_mode="Individual",
			class_language="English", campus="Indooroopilly", description="Paint",
			minimum_age=12, maximum_age=99, standard_price=200, capacity=15,
			materials_or_preparation="Brushes", inclusions="Materials",
		)
		required_doc.return_value = source
		workshop_frappe.get_all.return_value = [
			frappe._dict(session_date="2026-09-21", start_time="9:30:00", end_time="16:00:00", teacher="TEA-1", classroom="Room 1", status="Scheduled"),
			frappe._dict(session_date="2026-09-22", start_time="9:30:00", end_time="16:00:00", teacher=None, classroom=None, status="Scheduled"),
		]
		copy = Mock(name="copy")
		copy.name = "WSO-COPY"
		copy.set.side_effect = lambda field, value: setattr(copy, field, value)
		session_one = Mock(name="session_one")
		session_one.set.side_effect = lambda field, value: setattr(session_one, field, value)
		session_two = Mock(name="session_two")
		session_two.set.side_effect = lambda field, value: setattr(session_two, field, value)
		workshop_frappe.new_doc.side_effect = [copy, session_one, session_two]

		result = duplicate_school_admin_workshop_offering_data("WSO-ORIGINAL")

		self.assertEqual(copy.title, "Copy of Watercolour Day")
		self.assertEqual(copy.status, "Draft")
		self.assertEqual(copy.standard_price, 200)
		self.assertEqual(copy.capacity, 15)
		copy.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(session_one.workshop_offering, "WSO-COPY")
		self.assertEqual(session_one.teacher, "TEA-1")
		self.assertIsNone(session_two.teacher)
		session_one.insert.assert_called_once_with(ignore_permissions=True)
		session_two.insert.assert_called_once_with(ignore_permissions=True)
		sync_positions.assert_called_once_with("WSO-COPY")
		workshop_frappe.db.commit.assert_called_once()
		workshop_frappe.db.rollback.assert_not_called()
		self.assertEqual(result["source"], "WSO-ORIGINAL")
		self.assertEqual(result["offering"], {"name": "WSO-COPY"})
		build_detail.assert_called_once_with(copy)
		self.assertEqual(workshop_frappe.get_all.call_args.args[0], "Workshop Session")

	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_duplicate_rolls_back_when_session_copy_fails(self, _translate, workshop_frappe, required_doc, _require):
		required_doc.return_value = frappe._dict(name="WSO-ORIGINAL", title="Workshop")
		workshop_frappe.get_all.return_value = [frappe._dict(session_date="2026-09-21")]
		copy = Mock(name="copy")
		copy.name = "WSO-COPY"
		copy.set.side_effect = lambda field, value: setattr(copy, field, value)
		session = Mock(name="session")
		session.set.side_effect = lambda field, value: setattr(session, field, value)
		session.insert.side_effect = RuntimeError("session failed")
		workshop_frappe.new_doc.side_effect = [copy, session]

		with self.assertRaisesRegex(RuntimeError, "session failed"):
			duplicate_school_admin_workshop_offering_data("WSO-ORIGINAL")

		workshop_frappe.db.rollback.assert_called_once_with(save_point="duplicate_workshop_offering")
		workshop_frappe.db.commit.assert_not_called()

	@patch("qas_custom.services.workshops._require_school_admin")
	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops.get_student_parent_name", return_value="Student One")
	def test_activation_creates_only_missing_attendance_rows(
		self, _student_name, workshop_frappe, required_doc, _require
	):
		enrollment = Mock()
		enrollment.name = "WEN-1"
		enrollment.status = "Planned"
		enrollment.workshop_offering = "WSO-1"
		enrollment.student = "STU-1"
		enrollment.get.side_effect = lambda field: {
			"name": "WEN-1", "workshop_offering": "WSO-1", "student": "STU-1", "status": enrollment.status
		}.get(field)
		required_doc.return_value = enrollment
		workshop_frappe.get_all.return_value = [SimpleNamespace(name="WSS-1"), SimpleNamespace(name="WSS-2")]
		workshop_frappe.db.exists.side_effect = [True, False]
		attendance = Mock()
		workshop_frappe.get_doc.return_value = attendance

		result = activate_school_admin_workshop_enrollment_data("WEN-1")

		self.assertEqual(result["attendance_entries"], 1)
		self.assertEqual(enrollment.status, "Active")
		enrollment.save.assert_called_once_with(ignore_permissions=True)
		attendance.insert.assert_called_once_with(ignore_permissions=True)

	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_attendance_rejects_status_from_recurring_leave_workflow(self, _translate, workshop_frappe, required_doc):
		row = Mock()
		row.workshop_session = "WSS-1"
		required_doc.return_value = row
		workshop_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			_update_attendance("WSS-1", [{"row_id": "WAT-1", "status": "Leave"}])

		self.assertNotIn("Leave", ATTENDANCE_STATUSES)

	@patch("qas_custom.services.workshops._required_doc")
	@patch("qas_custom.services.workshops.frappe")
	@patch("qas_custom.services.workshops._", side_effect=lambda value: value)
	def test_attendance_rejects_row_from_another_workshop_session(self, _translate, workshop_frappe, required_doc):
		row = Mock()
		row.workshop_session = "WSS-OTHER"
		required_doc.return_value = row
		workshop_frappe.throw.side_effect = frappe.PermissionError

		with self.assertRaises(frappe.PermissionError):
			_update_attendance("WSS-1", [{"row_id": "WAT-1", "status": "Present"}])
