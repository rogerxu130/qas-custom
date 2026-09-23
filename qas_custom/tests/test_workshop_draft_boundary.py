from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.services.workshops import create_school_admin_workshop_invoice_data


MODULE = "qas_custom.services.workshops"


class _Meta:
	def has_field(self, _fieldname):
		return True


class _Invoice(frappe._dict):
	meta = _Meta()

	def __init__(self, **values):
		super().__init__(items=[], payment_schedule=[], grand_total=180, **values)
		self.insert = Mock()
		self.save = Mock()
		self.submit = Mock()

	def set(self, fieldname, value):
		self[fieldname] = value

	def append(self, fieldname, values):
		row = _InvoiceRow(values)
		self[fieldname].append(row)
		return row


class _InvoiceRow(frappe._dict):
	meta = _Meta()

	def set(self, fieldname, value):
		self[fieldname] = value


class TestWorkshopDraftBoundary(TestCase):
	def _run(self, *, existing_invoice=None, duplicate_item=False):
		enrollment = frappe._dict(
			name="WEN-1", status="Active", parent="PAR-1", student="STU-1",
			workshop_offering="WSO-1", standard_price_snapshot=180, invoice=None,
		)
		enrollment.save = Mock()
		offering = frappe._dict(name="WSO-1", title="Spring Painting", campus="South Bank")
		new_invoice = _Invoice(name="SINV-NEW")
		invoice = existing_invoice or new_invoice
		if duplicate_item:
			invoice["items"].append(_InvoiceRow(qas_line_type="Workshop", enrollment="WEN-1"))
		frappe_stub = SimpleNamespace(
			db=SimpleNamespace(get_value=Mock(return_value=None), exists=Mock(return_value=False), commit=Mock()),
			get_all=Mock(return_value=["2026-10-02", "2026-10-03"]),
			get_doc=Mock(return_value=invoice), new_doc=Mock(return_value=new_invoice),
		)
		with ExitStack() as stack:
			stack.enter_context(patch(MODULE + ".frappe", frappe_stub))
			permission = stack.enter_context(patch(MODULE + "._require_school_admin"))
			required = stack.enter_context(patch(MODULE + "._required_doc", side_effect=[enrollment, offering]))
			stack.enter_context(patch(MODULE + ".get_invoice_customer", return_value="CUS-1"))
			stack.enter_context(patch(MODULE + "._workshop_invoice_item", return_value="Workshop Fee"))
			stack.enter_context(patch(MODULE + ".get_student_parent_name", return_value="Amy"))
			stack.enter_context(patch(MODULE + ".get_student_display_code", return_value="AMY-01"))
			stack.enter_context(patch(MODULE + "._enrollment_payload", side_effect=lambda row: {"invoice": row.invoice}))
			guard = stack.enter_context(patch(MODULE + ".disable_sales_invoice_auto_notifications"))
			find = stack.enter_context(patch(MODULE + "._find_draft_workshop_invoice", return_value=existing_invoice.name if existing_invoice else None))
			date_init = lambda doc: doc.update(posting_date="2026-09-23", due_date="2026-09-30")
			production_globals = create_school_admin_workshop_invoice_data.__globals__
			if "apply_default_invoice_dates" in production_globals:
				stack.enter_context(patch(MODULE + ".apply_default_invoice_dates", side_effect=date_init))
			stack.enter_context(patch("qas_custom.modules.billing.drafts.frappe", frappe_stub))
			shared_date = stack.enter_context(patch("qas_custom.modules.billing.drafts.apply_default_invoice_dates", side_effect=date_init))
			factory = Mock()
			if "new_invoice_draft" in production_globals:
				factory = stack.enter_context(patch(MODULE + ".new_invoice_draft", wraps=production_globals["new_invoice_draft"]))
			sync = stack.enter_context(patch(MODULE + ".sync_invoice_student_summary"))
			snapshot = stack.enter_context(patch(MODULE + ".apply_invoice_payment_snapshot"))
			mutation = stack.enter_context(patch(MODULE + ".run_invoice_mutation_as_administrator", side_effect=lambda callback: callback()))
			result = create_school_admin_workshop_invoice_data("WEN-1")
			permission.assert_called_once_with()
			required.assert_has_calls([call("Workshop Enrollment", "WEN-1"), call("Workshop Offering", "WSO-1")])
			guard.assert_called_once_with()
			find.assert_called_once_with(parent="PAR-1", customer="CUS-1")
			frappe_stub.db.get_value.assert_called_once_with(
				"Sales Invoice", {"source_doctype": "Workshop Enrollment", "source_document": "WEN-1", "docstatus": ["<", 2]}, "name"
			)
			self.assertEqual(result["invoice"], invoice.name)
			self.assertEqual(result["reused"], bool(existing_invoice))
			self.assertEqual(result["enrollment"]["invoice"], invoice.name)
			self.assertEqual(enrollment.invoice, invoice.name)
			self.assertEqual(enrollment.invoice_status, "Draft")
			self.assertEqual(enrollment.invoice_amount, invoice.grand_total)
			enrollment.save.assert_called_once_with(ignore_permissions=True)
			frappe_stub.db.commit.assert_called_once_with()
			invoice.submit.assert_not_called()
			return SimpleNamespace(invoice=invoice, frappe=frappe_stub, shared_date=shared_date, factory=factory, sync=sync, snapshot=snapshot, mutation=mutation)

	def test_new_workshop_invoice_keeps_fields_item_and_persistence(self):
		result = self._run()
		invoice = result.invoice
		result.frappe.new_doc.assert_called_once_with("Sales Invoice")
		result.frappe.get_doc.assert_not_called()
		self.assertEqual(invoice.customer, "CUS-1")
		self.assertEqual(invoice.posting_date, "2026-09-23")
		self.assertEqual(invoice.due_date, "2026-09-30")
		self.assertEqual(invoice.parent, "PAR-1")
		self.assertEqual(invoice.qas_invoice_type, "Workshop")
		self.assertEqual(invoice.source_doctype, "Workshop Enrollment")
		self.assertEqual(invoice.source_document, "WEN-1")
		self.assertEqual(invoice.primary_student, "STU-1")
		self.assertIn("Draft Workshop invoice", invoice.billing_note)
		self._assert_new_line(invoice["items"][0])
		result.sync.assert_called_once_with(invoice)
		result.snapshot.assert_called_once_with(invoice)
		result.mutation.assert_called_once()
		invoice.insert.assert_called_once_with(ignore_permissions=True)
		invoice.save.assert_not_called()

	def test_existing_draft_preserves_adjustments_and_appends_line(self):
		old_line = _InvoiceRow(item_code="Existing Fee", rate=90)
		invoice = _Invoice(name="SINV-EXISTING", due_date="2026-11-15", discount_amount=25, customer="CUS-1", parent="PAR-1", qas_invoice_type="Workshop")
		invoice["items"].append(old_line)
		result = self._run(existing_invoice=invoice)
		result.frappe.get_doc.assert_called_once_with("Sales Invoice", "SINV-EXISTING")
		result.frappe.new_doc.assert_not_called()
		self.assertEqual(invoice.due_date, "2026-11-15")
		self.assertEqual(invoice.discount_amount, 25)
		self.assertEqual(invoice.customer, "CUS-1")
		self.assertEqual(invoice.parent, "PAR-1")
		self.assertEqual(invoice.qas_invoice_type, "Workshop")
		self.assertIs(invoice["items"][0], old_line)
		self.assertEqual(len(invoice["items"]), 2)
		self._assert_new_line(invoice["items"][1])
		result.sync.assert_called_once_with(invoice)
		result.snapshot.assert_called_once_with(invoice)
		result.mutation.assert_called_once()
		invoice.save.assert_called_once_with(ignore_permissions=True)
		invoice.insert.assert_not_called()

	def test_new_uses_shared_factory_and_reuse_does_not(self):
		created = self._run()
		created.factory.assert_called_once_with(customer="CUS-1", parent="PAR-1", invoice_type="Workshop")
		created.shared_date.assert_called_once_with(created.invoice)
		reused = self._run(existing_invoice=_Invoice(name="SINV-EXISTING", due_date="2026-11-15"))
		reused.factory.assert_not_called()
		reused.shared_date.assert_not_called()

	def test_existing_line_only_relinks_enrollment_without_invoice_mutation(self):
		invoice = _Invoice(name="SINV-EXISTING", due_date="2026-11-15", discount_amount=25)
		result = self._run(existing_invoice=invoice, duplicate_item=True)
		self.assertEqual(len(invoice["items"]), 1)
		result.sync.assert_not_called()
		result.snapshot.assert_not_called()
		result.mutation.assert_not_called()
		invoice.insert.assert_not_called()
		invoice.save.assert_not_called()

	def _assert_new_line(self, row):
		self.assertEqual(row.item_code, "Workshop Fee")
		self.assertEqual(row.item_name, "Spring Painting")
		self.assertEqual(row.description, "Spring Painting\nAmy\nSouth Bank\n2026-10-02, 2026-10-03")
		self.assertEqual(row.qty, 1)
		self.assertEqual(row.rate, 180)
		self.assertEqual(row.qas_line_type, "Workshop")
		self.assertEqual(row.enrollment, "WEN-1")
		self.assertEqual(row.student, "STU-1")
		self.assertEqual(row.student_display_name, "Amy")
		self.assertEqual(row.student_code, "AMY-01")
