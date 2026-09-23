from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.modules.billing.presentation import build_parent_invoice_context
from qas_custom.modules.notifications.commands import _invoice_pdf_html
from qas_custom.services.school_admin import (
	_apply_school_admin_draft_invoice_payload,
	_apply_invoice_adjustments,
	_apply_invoice_items,
	_invoice_edit_totals,
	update_school_admin_draft_invoice_data,
	delete_school_admin_draft_invoice_data,
	submit_school_admin_invoice_data,
)


class _Meta:
	def has_field(self, _fieldname):
		return True


class _Child(frappe._dict):
	meta = _Meta()

	def set(self, fieldname, value):
		self[fieldname] = value


class _Invoice(frappe._dict):
	meta = _Meta()

	def set(self, fieldname, value):
		self[fieldname] = value

	def append(self, fieldname, values):
		child = _Child(values)
		self.setdefault(fieldname, []).append(child)
		return child

	def remove(self, row):
		self["taxes"].remove(row)

	def calculate_taxes_and_totals(self):
		return None

	def submit(self):
		self.docstatus = 1


class TestSchoolAdminDraftInvoiceAdjustments(TestCase):
	def test_payg_draft_delete_has_explicit_business_error_without_detach(self):
		invoice = _Invoice(name="SINV-PAYG", docstatus=0, customer="CUS-1", parent="PAR-1",
			qas_invoice_type="PAYG Card", items=[_Child(name="ROW-1", qas_source_doctype="QAS PAYG Operation",
				qas_source_document="OP-1")])
		operation = frappe._dict(name="OP-1", invoice=invoice.name, family_parent="PAR-1", customer="CUS-1")
		fake_db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), commit=Mock())
		fake_frappe = SimpleNamespace(db=fake_db, delete_doc=Mock(),
			throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)))
		order = []
		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin._require_invoice_cancellation_actor"
		), patch(
			"qas_custom.services.school_admin.lock_payg_operations_for_invoices",
			side_effect=lambda _names: (order.append("operation"), {"OP-1": operation})[1]
		), patch(
			"qas_custom.services.school_admin.reject_payg_support_view_write"
		), patch(
			"qas_custom.services.school_admin._lock_school_admin_draft_invoice",
			side_effect=lambda _name: (order.append("invoice"), invoice)[1]
		), patch(
			"qas_custom.services.school_admin._detach_invoice_operation_report_links"
		) as detach, patch(
			"qas_custom.services.school_admin._clear_deleted_invoice_enrollment_snapshot"
		) as clear:
			with self.assertRaisesRegex(ValueError, "PAYG.*cannot be deleted"):
				delete_school_admin_draft_invoice_data("SINV-PAYG")
		self.assertEqual(order, ["operation", "invoice"])
		detach.assert_not_called()
		clear.assert_not_called()
		fake_frappe.delete_doc.assert_not_called()
		fake_db.commit.assert_not_called()

	def test_support_view_cannot_delete_linked_payg_draft(self):
		from qas_custom.modules.billing import payg_drafts
		fake_frappe = SimpleNamespace(db=SimpleNamespace(commit=Mock()), delete_doc=Mock())
		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin._require_invoice_cancellation_actor"
		), patch(
			"qas_custom.services.school_admin.lock_payg_operations_for_invoices", return_value={"OP-1": object()}
		), patch(
			"qas_custom.services.school_admin._lock_school_admin_draft_invoice"
		) as invoice_lock, patch.object(
			payg_drafts, "get_support_view_token", return_value="support-token"
		), patch.object(
			payg_drafts.frappe, "throw", side_effect=lambda message, *_args: (_ for _ in ()).throw(PermissionError(message))
		):
			with self.assertRaisesRegex(PermissionError, "Support View"):
				delete_school_admin_draft_invoice_data("SINV-PAYG")
		invoice_lock.assert_not_called()
		fake_frappe.delete_doc.assert_not_called()
		fake_frappe.db.commit.assert_not_called()
	def test_support_view_blocks_payg_draft_save_and_submit_before_invoice_lock(self):
		from qas_custom.modules.billing import payg_drafts
		fake_db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), commit=Mock())
		fake_frappe = SimpleNamespace(db=fake_db)
		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin._require_school_admin"
		), patch(
			"qas_custom.services.school_admin.lock_payg_operations_for_invoices", return_value={"OP-1": object()}
		), patch(
			"qas_custom.services.school_admin._lock_school_admin_draft_invoice"
		) as invoice_lock, patch.object(
			payg_drafts, "get_support_view_token", return_value="support-token"
		), patch.object(
			payg_drafts.frappe, "throw", side_effect=lambda message, *_args: (_ for _ in ()).throw(PermissionError(message))
		):
			for action in (update_school_admin_draft_invoice_data, submit_school_admin_invoice_data):
				with self.assertRaisesRegex(PermissionError, "Support View"):
					action("SINV-PAYG", payload={})
		invoice_lock.assert_not_called()
		fake_db.commit.assert_not_called()
		self.assertEqual(fake_db.rollback.call_count, 2)

	def test_payg_save_and_submit_keep_line_source_and_family(self):
		from contextlib import ExitStack
		invoice = _Invoice(name="SINV-PAYG", docstatus=0, customer="CUS-1", parent="PAR-1",
			qas_invoice_type="PAYG Card", grand_total=400, taxes=[], payment_schedule=[],
			items=[_Child(name="ROW-1", item_code="PAYG", description="Card", qty=1, rate=400,
				qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-1")])
		invoice.flags = SimpleNamespace(ignore_permissions=False)
		invoice.save = Mock()
		operation = frappe._dict(name="OP-1", invoice="SINV-PAYG", family_parent="PAR-1", customer="CUS-1")
		fake_db = SimpleNamespace(savepoint=Mock(), commit=Mock(), rollback=Mock())
		fake_frappe = SimpleNamespace(db=fake_db, get_doc=Mock(return_value=invoice),
			throw=Mock(side_effect=lambda message: (_ for _ in ()).throw(ValueError(message))))
		sequence = Mock()
		def lock_ops(_names):
			sequence.operations()
			return {"OP-1": operation}
		def lock_invoice(_name):
			sequence.invoice()
			return invoice
		payload = {"items": [{"name": "ROW-1", "item_code": "PAYG", "description": "Edited card",
			"qty": 1, "rate": 380}]}
		# Parent Portal SchoolAdminView.vue must pass `name: item.name` in its
		# invoice-item mapping. A missing stable child ID is rejected so an
		# editor cannot silently reassign a merged PAYG operation line.
		patches = [
			patch("qas_custom.services.school_admin.frappe", fake_frappe),
			patch("qas_custom.services.school_admin._require_school_admin"),
			patch("qas_custom.services.school_admin._", side_effect=lambda value: value),
			patch("qas_custom.services.school_admin.lock_payg_operations_for_invoices", side_effect=lock_ops),
			patch("qas_custom.services.school_admin._lock_school_admin_draft_invoice", side_effect=lock_invoice),
			patch("qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False),
			patch("qas_custom.services.school_admin._run_school_admin_invoice_mutation", side_effect=lambda callback: callback()),
			patch("qas_custom.services.school_admin._add_comment"),
			patch("qas_custom.services.school_admin._build_invoice_payload", return_value={"name": "SINV-PAYG"}),
			patch("qas_custom.services.school_admin.apply_store_credit_to_invoice", return_value={"applied": 0}),
			patch("qas_custom.services.school_admin.sync_invoice_store_credit_snapshot"),
			patch("qas_custom.services.school_admin.get_invoice_store_credit_applied", return_value=0),
			patch("qas_custom.services.school_admin._skipped_invoice_notification", return_value={"status": "Skipped"}),
		]
		with ExitStack() as stack:
			for item in patches:
				stack.enter_context(item)
			update_school_admin_draft_invoice_data("SINV-PAYG", payload)
			self.assertEqual((invoice.get("items")[0].qas_source_doctype,
				invoice.get("items")[0].qas_source_document), ("QAS PAYG Operation", "OP-1"))
			self.assertEqual(invoice.get("items")[0].rate, 380)
			self.assertEqual(sequence.mock_calls[:2], [call.operations(), call.invoice()])
			sequence.reset_mock()
			submit_school_admin_invoice_data("SINV-PAYG", payload=payload, send_notifications=False)
			self.assertEqual(invoice.docstatus, 1)
			self.assertEqual(sequence.mock_calls[:2], [call.operations(), call.invoice()])
			with self.assertRaisesRegex(ValueError, "customer and family"):
				_apply_school_admin_draft_invoice_payload(invoice, {"customer": "CUS-OTHER"})

	def test_payg_editor_rejects_removed_or_forged_source(self):
		invoice = _Invoice(name="SINV-PAYG", customer="CUS-1", parent="PAR-1", qas_invoice_type="PAYG Card",
			grand_total=400, taxes=[], items=[_Child(name="ROW-1", item_code="PAYG", description="Card",
				qty=1, rate=400, qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-1")])
		with patch("qas_custom.services.school_admin.frappe.throw", side_effect=lambda message: (_ for _ in ()).throw(ValueError(message))), patch(
			"qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False
		):
			with self.assertRaisesRegex(ValueError, "removed"):
				_apply_school_admin_draft_invoice_payload(invoice, {"items": [{"item_code": "PAYG", "description": "Card", "qty": 1, "rate": 400}]})
		invoice.set("items", [_Child(name="ROW-1", item_code="PAYG", description="Card", qty=1,
			rate=400, qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-1")])
		with patch("qas_custom.services.school_admin.frappe.throw", side_effect=lambda message: (_ for _ in ()).throw(ValueError(message))), patch(
			"qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False
		):
			with self.assertRaisesRegex(ValueError, "changed"):
				_apply_school_admin_draft_invoice_payload(invoice, {"items": [{"name": "ROW-1", "item_code": "PAYG",
					"description": "Card", "qty": 1, "rate": 400, "qas_source_document": "OP-OTHER"}]})
		invoice.set("items", [_Child(name="ROW-1", item_code="PAYG", description="Card", qty=1,
			rate=400, qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-1")])
		with patch("qas_custom.services.school_admin.frappe.throw", side_effect=lambda message: (_ for _ in ()).throw(ValueError(message))), patch(
			"qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False
		):
			with self.assertRaisesRegex(ValueError, "item, quantity"):
				_apply_school_admin_draft_invoice_payload(invoice, {"items": [{"name": "ROW-1", "item_code": "OTHER",
					"description": "Card", "qty": 1, "rate": 400}]})
	@patch("qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False)
	def test_edited_due_date_survives_erpnext_save_and_submit_recalculation(self, _snapshot):
		from erpnext.controllers.accounts_controller import AccountsController
		invoice = _Invoice(name="2600001-1", amended_from="2600001", docstatus=0,
			grand_total=68, items=[], taxes=[], due_date="2026-09-22",
			payment_schedule=[_Child(due_date="2026-09-22", payment_amount=68)])
		_apply_school_admin_draft_invoice_payload(invoice, {"due_date": "2026-09-30"})
		AccountsController.set_due_date(invoice)
		self.assertEqual(invoice.due_date, "2026-09-30")
		self.assertEqual(invoice.payment_schedule[0].due_date, "2026-09-30")
		AccountsController.set_due_date(invoice)
		self.assertEqual(invoice.due_date, "2026-09-30")
		self.assertEqual(invoice.payment_schedule[0].payment_amount, 68)

	@patch("qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False)
	def test_unrelated_edit_preserves_payment_schedule(self, _snapshot):
		invoice = _Invoice(grand_total=68, items=[], taxes=[], due_date="2026-09-22",
			payment_schedule=[_Child(due_date="2026-09-22")])
		_apply_school_admin_draft_invoice_payload(invoice, {"remarks": "Updated"})
		self.assertEqual(invoice.payment_schedule[0].due_date, "2026-09-22")

	def test_date_edit_does_not_flatten_installments(self):
		invoice = _Invoice(grand_total=68, items=[], taxes=[], due_date="2026-09-22",
			payment_schedule=[_Child(due_date="2026-09-18"), _Child(due_date="2026-09-22")])
		with patch("qas_custom.services.school_admin.frappe.throw", side_effect=frappe.ValidationError):
			with self.assertRaises(frappe.ValidationError):
				_apply_school_admin_draft_invoice_payload(invoice, {"due_date": "2026-09-30"})
		self.assertEqual([r.due_date for r in invoice.payment_schedule], ["2026-09-18", "2026-09-22"])

	def test_submit_uses_current_editor_payload_without_prior_draft_save(self):
		invoice = _Invoice(name="SINV-0001", docstatus=0)
		invoice.flags = SimpleNamespace(ignore_permissions=False)
		current_payload = {
			"qas_additional_description": "Current editor text",
			"items": [{"item_code": "Tuition Fee", "description": "Current description", "qty": 1, "rate": 68}],
		}
		fake_db = SimpleNamespace(savepoint=Mock(), commit=Mock(), rollback=Mock())
		fake_frappe = SimpleNamespace(db=fake_db, get_doc=Mock(return_value=invoice))

		with patch("qas_custom.services.school_admin.frappe", fake_frappe), patch(
			"qas_custom.services.school_admin._require_school_admin"
		), patch(
			"qas_custom.services.school_admin.lock_payg_operations_for_invoices", return_value={}
		), patch(
			"qas_custom.services.school_admin.validate_payg_bindings", return_value={}
		), patch(
			"qas_custom.services.school_admin._", side_effect=lambda message: message
		), patch(
			"qas_custom.services.school_admin._lock_school_admin_draft_invoice", return_value=invoice
		), patch(
			"qas_custom.services.school_admin._apply_school_admin_draft_invoice_payload",
			return_value={"previous_total": 60, "new_total": 68, "item_count": 1, "adjustment_count": 0},
		) as apply_payload, patch(
			"qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False
		), patch(
			"qas_custom.services.school_admin._run_school_admin_invoice_mutation", side_effect=lambda callback: callback()
		), patch(
			"qas_custom.services.school_admin._add_comment"
		), patch(
			"qas_custom.services.school_admin.apply_store_credit_to_invoice", return_value={"applied": 0}
		), patch(
			"qas_custom.services.school_admin.sync_invoice_store_credit_snapshot"
		), patch(
			"qas_custom.services.school_admin.get_invoice_store_credit_applied", return_value=0
		), patch(
			"qas_custom.services.school_admin._skipped_invoice_notification", return_value={"status": "Skipped"}
		), patch(
			"qas_custom.services.school_admin._build_invoice_payload", return_value={"name": "SINV-0001"}
		):
			result = submit_school_admin_invoice_data(
				invoice="SINV-0001",
				payload=current_payload,
				send_notifications=False,
			)

		apply_payload.assert_called_once_with(invoice, current_payload)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(result["name"], "SINV-0001")

	@patch("qas_custom.services.school_admin.apply_invoice_payment_snapshot", return_value=False)
	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_current_editor_additional_description_is_applied_before_submit(self, _has_field, _payment_snapshot):
		invoice = _Invoice(
			name="SINV-0001",
			grand_total=68,
			items=[],
			taxes=[],
		)

		change = _apply_school_admin_draft_invoice_payload(
			invoice,
			{
				"qas_additional_description": "Capacity-building support delivered through a small-group visual arts session.",
				"remarks": "NDIS invoice",
			},
		)

		self.assertEqual(
			invoice.qas_additional_description,
			"Capacity-building support delivered through a small-group visual arts session.",
		)
		self.assertEqual(invoice.remarks, "NDIS invoice")
		self.assertEqual(change["previous_total"], 68)

	def test_zero_unit_price_remains_zero(self):
		invoice = _Invoice(items=[])

		_apply_invoice_items(
			invoice,
			[
				{
					"item_code": "Tuition Fee",
					"description": "Complimentary class",
					"qty": 2,
					"rate": 0,
				}
			],
		)

		self.assertEqual(invoice.get("items")[0].qty, 2)
		self.assertEqual(invoice.get("items")[0].rate, 0)

	@patch("qas_custom.services.school_admin._has_field", return_value=True)
	def test_adjustments_replace_only_qas_adjustment_rows(self, _has_field):
		ordinary_tax = _Child(
			description="GST",
			tax_amount=10,
			account_head="GST Payable",
			qas_is_invoice_adjustment=0,
		)
		old_adjustment = _Child(
			description="Old discount",
			tax_amount=-20,
			account_head="Tuition Income",
			cost_center="Main - QAS",
			qas_is_invoice_adjustment=1,
		)
		invoice = _Invoice(
			company="Queensland Art School",
			items=[_Child(income_account="Tuition Income", cost_center="Main - QAS")],
			taxes=[ordinary_tax, old_adjustment],
		)

		_apply_invoice_adjustments(
			invoice,
			[
				{"description": "Sibling discount", "amount": -30},
				{"description": "Materials", "amount": 15},
			],
		)

		self.assertIn(ordinary_tax, invoice.get("taxes"))
		self.assertNotIn(old_adjustment, invoice.get("taxes"))
		self.assertEqual(len(invoice.get("taxes")), 3)
		self.assertEqual(
			[(row.description, row.tax_amount) for row in invoice.get("taxes")[1:]],
			[("Sibling discount", -30), ("Materials", 15)],
		)
		self.assertTrue(all(row.qas_is_invoice_adjustment for row in invoice.get("taxes")[1:]))

	def test_edit_totals_separate_items_adjustments_and_other_charges(self):
		invoice = _Invoice(
			items=[_Child(amount=400), _Child(amount=80)],
			taxes=[
				_Child(tax_amount=-30, qas_is_invoice_adjustment=1),
				_Child(tax_amount=10, qas_is_invoice_adjustment=0),
			],
			total_taxes_and_charges=-20,
		)

		self.assertEqual(
			_invoice_edit_totals(invoice),
			{
				"item_subtotal": 480,
				"adjustment_total": -30,
				"other_charge_total": 10,
			},
		)

	@patch("qas_custom.modules.billing.presentation.get_invoice_settings", return_value={})
	@patch("qas_custom.modules.billing.presentation.get_invoice_payment_context", return_value={})
	@patch("qas_custom.modules.billing.presentation.get_invoice_total_amount", return_value=450)
	@patch("qas_custom.services.stripe_trial_payments.payment_url", return_value="https://example.invalid/pay/SINV-0001")
	@patch(
		"qas_custom.modules.billing.presentation.resolve_invoice_print_amounts",
		return_value={"store_credit_applied": 0, "payable_amount": 450},
	)
	@patch("qas_custom.modules.billing.presentation._invoice_recipient_name", return_value="Taylor")
	@patch("qas_custom.modules.billing.payment_plans.payment_plan_payload", return_value={})
	def test_parent_context_exposes_adjustment_as_independent_line(
		self,
		_payment_plan,
		_recipient_name,
		_amounts,
		_payment_url,
		_total,
		_payment_context,
		_settings,
	):
		invoice = _Invoice(
			name="SINV-0001",
			customer_name="Taylor Family",
			qas_additional_description="Capacity-building support delivered through a small-group visual arts session.",
			items=[
				_Child(
					item_code="Tuition Fee",
					description="Term 3 course",
					student_display_name="Alex",
					qty=8,
					rate=60,
					amount=480,
				)
			],
			taxes=[
				_Child(
					description="Sibling discount",
					tax_amount=-30,
					qas_is_invoice_adjustment=1,
				)
			],
		)

		context = build_parent_invoice_context(invoice, include_portal_link=False)

		self.assertEqual(len(context["lines"]), 2)
		self.assertEqual(context["lines"][1]["description"], "Sibling discount")
		self.assertEqual(context["lines"][1]["amount"], -30)
		self.assertEqual(
			context["additional_description"],
			"Capacity-building support delivered through a small-group visual arts session.",
		)

	@patch("qas_custom.modules.notifications.commands._school_identity_pdf_html", return_value="")
	def test_parent_pdf_hides_unit_and_unit_price_and_shows_adjustment(self, _identity):
		html = _invoice_pdf_html(
			{
				"invoice": "SINV-0001",
				"school_name": "Queensland Art School",
				"due_date": "31 July 2026",
				"posting_date": "24 July 2026",
				"total": 450,
				"store_credit_applied": 0,
				"payable_amount": 450,
				"invoice_message": "",
				"additional_description": "Capacity-building support\nwith peers.",
				"accepted_payment_methods": "",
				"lines": [
					{"student": "Alex", "description": "Term 3 course", "amount": 480},
					{"student": "", "description": "Sibling discount", "amount": -30},
				],
			}
		)

		self.assertIn("Sibling discount", html)
		self.assertIn("Additional description", html)
		self.assertIn("Capacity-building support<br>with peers.", html)
		self.assertNotIn("Unit price", html)
		self.assertNotIn(">Qty<", html)
		self.assertNotIn(">Rate<", html)
