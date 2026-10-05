"""Purchase lifecycle transaction and financial-boundary regression checks."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.billing import payg_drafts
from qas_custom.modules.payg import issue
from qas_custom.services import school_admin


class Doc(frappe._dict):
    def save(self, **kwargs):
        return self

    def add_comment(self, *args):
        self.comments = [*(self.comments or []), args]

    def reload(self):
        return self


class TestPurchaseLifecycle(TestCase):
    def setUp(self):
        self.op = Doc(name="OP", operation_type="Purchase", family_parent="P", customer="CUS",
                      product="PROD", status="Pending", invoice=None, card=None)
        self.card = Doc(name="CARD", family_parent="P", customer="CUS", status="Active",
                        available_count=10, reserved_count=0, consumed_count=0)
        self.invoice = Doc(name="INV", customer="CUS", parent="P", docstatus=0,
                           qas_invoice_type="PAYG Card", items=[Doc(name="ROW", qas_source_doctype="QAS PAYG Operation", qas_source_document="OP")])
        self.entries = []
        self.snapshots = {}
        def get_doc(dt, name=None, **kwargs):
            if isinstance(dt, dict):
                entry = Doc(dt)
                def insert(**kwargs):
                    self.entries.append(entry)
                    self.card.available_count += entry.available_delta
                    return entry
                entry.insert = insert
                return entry
            return {"QAS PAYG Operation": self.op, "QAS PAYG Card": self.card, "Sales Invoice": self.invoice}[dt]
        def savepoint(name):
            self.snapshots[name] = deepcopy((dict(self.op), dict(self.card), self.entries))
        def rollback(save_point):
            op, card, entries = deepcopy(self.snapshots[save_point])
            self.op.clear(); self.op.update(op)
            self.card.clear(); self.card.update(card)
            self.entries[:] = entries
        self.fake = SimpleNamespace(
            get_doc=Mock(side_effect=get_doc), get_roles=Mock(return_value=["School Admin"]),
            session=SimpleNamespace(user="admin"), PermissionError=PermissionError,
            throw=lambda message, *args: (_ for _ in ()).throw(ValueError(message)),
            delete_doc=Mock(), db=SimpleNamespace(sql=Mock(), exists=Mock(return_value=False),
                savepoint=Mock(side_effect=savepoint), rollback=Mock(side_effect=rollback), commit=Mock()))
        for p in [patch.object(payg_drafts, "frappe", self.fake),
                  patch.object(payg_drafts, "get_support_view_token", return_value=None),
                  patch.object(payg_drafts, "run_invoice_mutation_as_administrator", side_effect=lambda fn: fn())]:
            p.start(); self.addCleanup(p.stop)

    def issued(self):
        self.op.card, self.op.invoice, self.op.status = "CARD", "INV", "Completed"

    def test_pending_cancel_has_no_card_invoice_or_ledger_writes_and_is_idempotent(self):
        for _ in range(2):
            result = payg_drafts.cancel_purchase("OP", "P", "Not proceeding")
            self.assertEqual(result["operation"].status, "Cancelled")
        self.assertEqual(len(self.op.comments), 1)
        self.assertEqual(self.entries, [])
        self.fake.delete_doc.assert_not_called()
        self.fake.db.commit.assert_not_called()

    def test_issued_cancel_removes_only_its_exclusive_draft_and_zeroes_card(self):
        self.issued()
        result = payg_drafts.cancel_purchase("OP", "P", "Wrong course", cancel_invoice=True)
        self.assertEqual((self.op.status, self.card.status, self.card.available_count), ("Cancelled", "Cancelled", 0))
        self.assertEqual(result["invoice_action"], "deleted")
        self.fake.delete_doc.assert_called_once_with("Sales Invoice", "INV", ignore_permissions=True)
        self.assertIsNone(self.op.invoice)
        self.assertEqual(self.entries[0].operation_key, "cancel-purchase:OP")
        self.assertIn("INV", self.op.comments[0][1])

    def test_issued_cancel_can_leave_invoice_untouched(self):
        self.issued()
        result = payg_drafts.cancel_purchase("OP", "P", "Cancelled", cancel_invoice=False)
        self.assertEqual(result["invoice_action"], "unchanged")
        self.assertEqual(self.op.invoice, "INV")
        self.fake.delete_doc.assert_not_called()
        self.assertEqual(self.invoice.docstatus, 0)

    def test_submitted_invoice_reuses_cancellation_without_an_early_commit(self):
        self.issued(); self.invoice.docstatus = 1
        with patch.object(school_admin, "cancel_school_admin_invoice_data", return_value={"cancellation_store_credit_amount": 120}) as cancel:
            result = payg_drafts.cancel_purchase("OP", "P", "Wrong course", cancel_invoice=True)
        cancel.assert_called_once_with(invoice="INV", reason="Wrong course", commit=False)
        self.assertEqual((result["invoice_action"], result["store_credit_amount"]), ("cancelled", 120))
        self.fake.db.commit.assert_not_called()

    def test_invoice_failure_rolls_back_card_ledger_and_operation(self):
        self.issued(); self.invoice.docstatus = 1
        with patch.object(school_admin, "cancel_school_admin_invoice_data", side_effect=RuntimeError("invoice failed")):
            with self.assertRaisesRegex(RuntimeError, "invoice failed"):
                payg_drafts.cancel_purchase("OP", "P", "Wrong course", cancel_invoice=True)
        self.assertEqual((self.op.status, self.card.status, self.card.available_count), ("Completed", "Active", 10))
        self.assertEqual(self.entries, [])

    def test_consolidated_invoice_is_not_cancelled_with_other_charges(self):
        self.issued(); self.invoice["items"].append(Doc(name="OTHER"))
        with self.assertRaisesRegex(ValueError, "other charges"):
            payg_drafts.cancel_purchase("OP", "P", "Wrong course", cancel_invoice=True)
        self.assertEqual(self.entries, [])
        self.fake.delete_doc.assert_not_called()

    def test_used_reserved_and_transferred_cards_are_rejected(self):
        self.issued()
        for field, value in (("available_count", 9), ("reserved_count", 1), ("consumed_count", 1), ("status", "Transferred")):
            old = self.card[field]; self.card[field] = value
            with self.assertRaisesRegex(ValueError, "unused"):
                payg_drafts.cancel_purchase("OP", "P", "Cancelled")
            self.card[field] = old
        self.fake.db.exists.return_value = True
        with self.assertRaisesRegex(ValueError, "Used, booked"):
            payg_drafts.cancel_purchase("OP", "P", "Cancelled")
        self.assertEqual(self.entries, [])

    def test_family_role_support_and_reason_checks(self):
        for family, reason in (("OTHER", "Cancelled"), ("P", "")):
            with self.assertRaises(ValueError):
                payg_drafts.cancel_purchase("OP", family, reason)
        with patch.object(payg_drafts, "get_support_view_token", return_value="support"), self.assertRaises(ValueError):
            payg_drafts.cancel_purchase("OP", "P", "Cancelled")
        self.fake.get_roles.return_value = ["Parent"]
        with self.assertRaises(ValueError):
            payg_drafts.cancel_purchase("OP", "P", "Cancelled")

    def test_issue_and_draft_are_atomic_and_retries_keep_one_invoice(self):
        def issue_card(op_id, key):
            self.op.card = "CARD"; self.op.issue_request_key = key
            return self.card
        def draft(op_id, key):
            self.op.invoice = "INV"; self.op.invoice_request_key = key
            return self.invoice
        with patch.object(issue, "issue_card", side_effect=issue_card) as issuing, patch.object(payg_drafts, "create_payg_draft", side_effect=draft) as drafting:
            for _ in range(2):
                result = payg_drafts.issue_purchase_with_draft("OP", "key")
                self.assertEqual((result.status, result.card, result.invoice), ("Completed", "CARD", "INV"))
            self.assertEqual(issuing.call_args.args, ("OP", "key"))
            self.assertEqual(drafting.call_args.args, ("OP", "issue-invoice:OP"))
        self.fake.db.commit.assert_not_called()

    def test_issue_rolls_back_when_invoice_creation_fails(self):
        def issue_card(op_id, key):
            self.op.card = "CARD"
            return self.card
        with patch.object(issue, "issue_card", side_effect=issue_card), patch.object(payg_drafts, "create_payg_draft", side_effect=RuntimeError("draft failed")):
            with self.assertRaisesRegex(RuntimeError, "draft failed"):
                payg_drafts.issue_purchase_with_draft("OP", "key")
        self.assertIsNone(self.op.card)
        self.assertIsNone(self.op.invoice)
        self.assertEqual(self.op.status, "Pending")
