"""PAYG draft command contracts without an ERPNext site."""
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.billing import payg_drafts


class Doc(frappe._dict):
    def save(self, **_kwargs):
        return self

    def insert(self, **_kwargs):
        self.name = "SINV-NEW"
        return self

    def append(self, _table, values):
        row = Doc(values)
        self.setdefault("items", []).append(row)
        return row


class TestPaygDrafts(TestCase):
    def setUp(self):
        self.operation = Doc(name="OP-1", operation_type="Purchase", status="Pending",
                             family_parent="P-1", customer="C-1", product="PROD-1",
                             invoice=None, invoice_request_key=None, card=None,
                             new_price=Decimal("40"), new_course="COURSE-1")
        self.product = Doc(name="PROD-1", course="COURSE-1", enabled=1,
                           standard_card_price=Decimal("400"), invoice_item="ITEM-1")
        self.invoice = Doc(name=None, items=[], customer="C-1", parent="P-1")
        self.invoice.qas_invoice_type = "PAYG Card"
        def get_doc(dt, name=None, **_kwargs):
            if dt == "QAS PAYG Operation": return self.operation
            if dt == "QAS PAYG Product": return self.product
            if dt == "Sales Invoice": return self.invoice
            raise AssertionError((dt, name))
        self.fake = SimpleNamespace(
            session=SimpleNamespace(user="admin@example.com"),
            get_roles=lambda _user: ["School Admin"], get_doc=Mock(side_effect=get_doc),
            get_all=Mock(return_value=[]),
            db=SimpleNamespace(savepoint=Mock(), rollback=Mock(), commit=Mock(), sql=Mock(),
                               get_value=Mock(return_value="C-1"), exists=Mock(return_value=True)),
            throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
            PermissionError=PermissionError)
        self.patches = [patch.object(payg_drafts, "frappe", self.fake),
                        patch.object(payg_drafts, "get_support_view_token", return_value=None),
                        patch.object(payg_drafts, "disable_sales_invoice_auto_notifications"),
                        patch.object(payg_drafts, "new_invoice_draft", return_value=self.invoice),
                        patch.object(payg_drafts, "apply_invoice_payment_snapshot"),
                        patch.object(payg_drafts, "run_invoice_mutation_as_administrator", side_effect=lambda f: f())]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def test_purchase_source_and_idempotency_without_card(self):
        invoice = payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.assertEqual((invoice.qas_invoice_type, invoice.get("items")[0].qas_line_type), ("PAYG Card", "PAYG Card"))
        self.assertEqual((invoice.get("items")[0].qas_source_doctype, invoice.get("items")[0].qas_source_document),
                         ("QAS PAYG Operation", "OP-1"))
        self.assertEqual((invoice.get("items")[0].qty, invoice.get("items")[0].rate), (1, Decimal("400")))
        self.assertEqual((self.operation.invoice, self.operation.invoice_request_key), ("SINV-NEW", "invoice-1"))
        self.assertIsNone(self.operation.card)
        self.assertIs(payg_drafts.create_payg_draft("OP-1", "invoice-1"), invoice)
        with self.assertRaisesRegex(ValueError, "another key"):
            payg_drafts.create_payg_draft("OP-1", "invoice-2")
        self.fake.db.commit.assert_not_called()

    def test_existing_invoice_requires_matching_line_source(self):
        payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.invoice.get("items")[0].qas_source_document = "OP-OTHER"
        with self.assertRaisesRegex(ValueError, "source"):
            payg_drafts.create_payg_draft("OP-1", "invoice-1")

    def test_linked_invoice_can_have_another_operation_line_after_consolidation(self):
        payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.invoice.append("items", {"qas_source_doctype": "QAS PAYG Operation",
                                      "qas_source_document": "OP-2"})
        self.assertIs(payg_drafts.create_payg_draft("OP-1", "invoice-1"), self.invoice)
        self.fake.get_doc.assert_any_call("Sales Invoice", "SINV-NEW", for_update=True)

    def test_exchange_positive_uses_delta_and_negative_has_no_invoice(self):
        self.operation.operation_type = "Exchange"
        self.operation.status = "Completed"
        self.operation.target_card = "CARD-2"
        self.operation.price_delta = Decimal("25.50")
        self.operation.new_course = "COURSE-1"
        self.operation.quantity = 3
        invoice = payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.assertEqual((invoice.get("items")[0].rate, invoice.get("items")[0].qas_line_type), (Decimal("25.50"), "PAYG Exchange"))
        self.operation.invoice = None
        self.operation.invoice_request_key = None
        self.operation.price_delta = Decimal("-10")
        result = payg_drafts.create_payg_draft("OP-1", "invoice-2")
        self.assertEqual(result["reason"], "non_positive_exchange_delta")
        self.assertIsNone(result["invoice"])

    def test_rejects_support_view_and_invalid_item(self):
        with patch.object(payg_drafts, "get_support_view_token", return_value="support"):
            with self.assertRaisesRegex(ValueError, "Support View"):
                payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.fake.db.exists.return_value = False
        with self.assertRaisesRegex(ValueError, "Invoice Item"):
            payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.assertIsNone(self.operation.invoice)
        self.fake.db.rollback.assert_called()

    def test_purchase_draft_rejects_changed_product_course(self):
        self.product.course = "COURSE-OTHER"
        with self.assertRaisesRegex(ValueError, "product/course"):
            payg_drafts.create_payg_draft("OP-1", "invoice-1")

    def test_support_view_guard_only_applies_to_linked_payg_invoices(self):
        with patch.object(payg_drafts, "get_support_view_token", return_value="support"):
            payg_drafts.reject_payg_support_view_write({})
            with self.assertRaisesRegex(ValueError, "Support View"):
                payg_drafts.reject_payg_support_view_write({"OP-1": self.operation})

    def test_prior_card_does_not_change_invoice_or_create_another_card(self):
        self.operation.card = "CARD-1"
        invoice = payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.assertEqual(self.operation.card, "CARD-1")
        self.assertEqual(invoice.get("items")[0].qas_source_document, "OP-1")
        self.fake.get_doc.assert_any_call("QAS PAYG Operation", "OP-1", for_update=True)

    def test_exchange_wrapper_rolls_back_on_draft_failure(self):
        state = {"operation": None, "target_card": None, "entries": []}
        snapshots = {}
        def savepoint(name):
            snapshots[name] = (state["operation"], state["target_card"], list(state["entries"]))
        def rollback(save_point):
            state["operation"], state["target_card"], state["entries"] = snapshots[save_point]
        def exchange(*_args, **_kwargs):
            state.update(operation="OP-1", target_card="CARD-2", entries=["Transfer Out", "Transfer In"])
            return self.operation
        self.fake.db.savepoint.side_effect = savepoint
        self.fake.db.rollback.side_effect = rollback
        with patch("qas_custom.modules.payg.card_admin.exchange_card", side_effect=exchange), \
                patch.object(payg_drafts, "create_payg_draft", side_effect=RuntimeError("invoice failed")):
            with self.assertRaisesRegex(RuntimeError, "invoice failed"):
                payg_drafts.exchange_card_with_draft("CARD-1", "PROD-1", "exchange-1", "invoice-1")
        self.fake.db.rollback.assert_called_once()
        self.assertIn("payg_exchange_invoice_", self.fake.db.rollback.call_args.kwargs["save_point"])
        self.assertEqual(state, {"operation": None, "target_card": None, "entries": []})

    def test_payg_operation_locks_are_sorted_before_invoice_mutation(self):
        first = Doc(name="SINV-1", qas_invoice_type="PAYG Card", items=[
            Doc(name="ROW-1", qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-Z")])
        second = Doc(name="SINV-2", qas_invoice_type="PAYG Card", items=[
            Doc(name="ROW-2", qas_source_doctype="QAS PAYG Operation", qas_source_document="OP-A")])
        order = []
        def get_doc(doctype, name, **kwargs):
            if doctype == "Sales Invoice":
                return {"SINV-1": first, "SINV-2": second}[name]
            order.append((name, kwargs.get("for_update")))
            return Doc(name=name)
        self.fake.get_doc.side_effect = get_doc
        payg_drafts.lock_payg_operations_for_invoices(["SINV-1", "SINV-2"])
        self.assertEqual(order, [("OP-A", True), ("OP-Z", True)])

    def test_linked_operation_without_invoice_line_is_detected(self):
        self.operation.invoice = "SINV-1"
        invoice = Doc(name="SINV-1", customer="C-1", parent="P-1", qas_invoice_type="Other", items=[])
        self.fake.get_all.return_value = ["OP-1"]
        self.fake.get_doc.side_effect = lambda dt, name, **_kwargs: invoice if dt == "Sales Invoice" else self.operation
        locked = payg_drafts.lock_payg_operations_for_invoices(["SINV-1"])
        with self.assertRaisesRegex(ValueError, "sources changed"):
            payg_drafts.validate_payg_bindings(invoice, locked)

    def test_existing_course_or_workshop_draft_is_never_queried(self):
        payg_drafts.create_payg_draft("OP-1", "invoice-1")
        self.fake.get_doc.assert_any_call("QAS PAYG Operation", "OP-1", for_update=True)
        self.assertFalse(any(c.args[0] == "Sales Invoice" for c in self.fake.get_doc.call_args_list))


class TestPaygInvoicePatch(TestCase):
    def test_options_are_appended_to_custom_field_and_property_setter_once(self):
        from qas_custom.patches import v2026_09_23_payg_invoice_fields as migration
        values = {
            ("Custom Field", "Sales Invoice", "qas_invoice_type"):
                frappe._dict(name="SI-type", options="Course\nWorkshop"),
            ("Property Setter", "Sales Invoice", "qas_invoice_type"):
                frappe._dict(name="PS-type", value="Course\nWorkshop"),
            ("Custom Field", "Sales Invoice Item", "qas_line_type"):
                frappe._dict(name="SII-type", options="Course Fee\nWorkshop"),
            ("Property Setter", "Sales Invoice Item", "qas_line_type"):
                frappe._dict(name="PS-line", value="Course Fee\nWorkshop"),
        }
        writes = []
        def get_all(doctype, filters, fields, limit):
            row = values.get((doctype, filters.get("dt") or filters.get("doc_type"),
                              filters.get("fieldname") or filters.get("field_name")))
            return [row] if row else []
        def set_value(doctype, name, field, value, **_kwargs):
            writes.append((doctype, name, field, value))
            for (kind, _dt, _field), row in values.items():
                if kind == doctype and row.name == name:
                    row[field] = value
        fake = SimpleNamespace(get_all=get_all, clear_cache=Mock(),
                               db=SimpleNamespace(set_value=set_value, exists=Mock(return_value=False)),
                               get_doc=Mock(return_value=SimpleNamespace(insert=Mock())))
        with patch.object(migration, "frappe", fake):
            migration.execute()
            migration.execute()
        self.assertEqual(len(writes), 4)
        self.assertEqual(values[("Custom Field", "Sales Invoice Item", "qas_line_type")].options,
                         "Course Fee\nWorkshop\nPAYG Card\nPAYG Exchange")
        self.assertNotIn("default", [write[2] for write in writes])


class TestPaygCrossModuleOrders(TestCase):
    def _run_order(self, invoice_first):
        from qas_custom.tests.test_payg_issue import TestIssue
        from qas_custom.modules.payg.issue import issue_card

        case = TestIssue()
        case.setUp()
        try:
            case.operation.invoice = None
            case.operation.invoice_request_key = None
            case.product.invoice_item = "ITEM-1"
            case.fake.db.exists = Mock(return_value=True)
            case.fake.db.commit = Mock()
            invoice = Doc(name=None, items=[], customer="C-1", parent="P-1", qas_invoice_type="PAYG Card")
            with patch.object(payg_drafts, "frappe", case.fake), \
                    patch.object(payg_drafts, "get_support_view_token", return_value=None), \
                    patch.object(payg_drafts, "disable_sales_invoice_auto_notifications"), \
                    patch.object(payg_drafts, "new_invoice_draft", return_value=invoice), \
                    patch.object(payg_drafts, "apply_invoice_payment_snapshot"), \
                    patch.object(payg_drafts, "run_invoice_mutation_as_administrator", side_effect=lambda f: f()):
                if invoice_first:
                    created = payg_drafts.create_payg_draft("OP-1", "invoice-1")
                    case.product.standard_card_price = Decimal("900")
                    card = issue_card("OP-1", "issue-1")
                else:
                    card = issue_card("OP-1", "issue-1")
                    case.product.standard_card_price = Decimal("900")
                    created = payg_drafts.create_payg_draft("OP-1", "invoice-1")
            self.assertIs(created, invoice)
            self.assertEqual(case.operation.invoice, "SINV-NEW")
            self.assertEqual(case.operation.card, card.name)
            self.assertEqual(case.operation.issue_request_key, "issue-1")
            self.assertEqual(case.operation.invoice_request_key, "invoice-1")
            self.assertEqual(invoice.get("items")[0].rate, Decimal("400.000000000"))
            self.assertEqual(card.unit_price_snapshot, Decimal("40.000000000"))
            case.fake.db.commit.assert_not_called()
        finally:
            case.doCleanups()

    def test_invoice_then_issue_uses_same_operation(self):
        self._run_order(True)

    def test_issue_then_invoice_uses_same_operation(self):
        self._run_order(False)
