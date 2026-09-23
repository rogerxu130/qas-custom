"""Issue command contracts with a small transactional document stand-in."""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.payg import issue


class Document(frappe._dict):
    def __init__(self, state, **values):
        super().__init__(values)
        self._state = state

    def insert(self, **_kwargs):
        if not self.get("name"):
            self.name = f"{self.doctype}-{len(self._state['docs']) + 1}"
        self._state["docs"][(self.doctype, self.name)] = self
        if self.doctype == "QAS PAYG Entry":
            card = self._state["docs"][("QAS PAYG Card", self.card)]
            for field in ("available", "reserved", "consumed"):
                count = field + "_count"
                card[count] = card.get(count, 0) + self.get(field + "_delta", 0)
        return self

    def save(self, **_kwargs):
        return self


class TestIssue(TestCase):
    def setUp(self):
        self.state = {"docs": {}}
        self.operation = Document(self.state, doctype="QAS PAYG Operation", name="OP-1",
                                  operation_type="Purchase", status="Pending", family_parent="P-1",
                                  customer="C-1", product="PROD-1", invoice="INV-1", card=None,
                                  issue_request_key=None)
        self.state["docs"][(self.operation.doctype, self.operation.name)] = self.operation
        self.product = Document(self.state, doctype="QAS PAYG Product", name="PROD-1",
                                enabled=1, course="COURSE-1", standard_card_price=Decimal("400"))
        self.state["docs"][(self.product.doctype, self.product.name)] = self.product
        def get_doc(dt, name=None, **_kwargs):
            if isinstance(dt, dict):
                return Document(self.state, **dt)
            return self.state["docs"][(dt, name)]
        self.fake = SimpleNamespace(
            session=SimpleNamespace(user="admin@example.com"),
            get_roles=lambda _user: ["School Admin"], get_doc=Mock(side_effect=get_doc),
            db=SimpleNamespace(get_value=Mock(side_effect=self.value), savepoint=Mock(), rollback=Mock(), sql=Mock()),
            throw=lambda message, *_args: (_ for _ in ()).throw(ValueError(message)),
            PermissionError=PermissionError, DuplicateEntryError=frappe.DuplicateEntryError,
            UniqueValidationError=frappe.UniqueValidationError,
        )
        self.patches = [patch.object(issue, "frappe", self.fake),
                        patch.object(issue, "_now", return_value=datetime(2026, 9, 24, 9))]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def value(self, dt, key, field):
        if dt == "Parent": return "C-1"
        if dt == "QAS PAYG Product" and field == "enabled": return 1
        return None

    def test_prior_invoice_and_same_issue_key_reuse_one_card(self):
        first = issue.issue_card("OP-1", "issue-1")
        again = issue.issue_card("OP-1", "issue-1")
        self.assertIs(first, again)
        self.assertEqual(self.operation.invoice, "INV-1")
        self.assertEqual((first.issued_on.isoformat(), first.expires_on.isoformat()), ("2026-09-24", "2027-03-24"))
        self.assertEqual(first.unit_price_snapshot, Decimal("40"))
        self.assertEqual(first.available_count, 10)
        entries = [doc for (dt, _), doc in self.state["docs"].items() if dt == "QAS PAYG Entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].available_delta, entries[0].operation_key), (10, "issue:OP-1"))
        with self.assertRaisesRegex(ValueError, "already issued"):
            issue.issue_card("OP-1", "issue-2")

    def test_school_admin_and_customer_are_required(self):
        self.fake.get_roles = lambda _user: []
        with self.assertRaisesRegex(ValueError, "School Admin"):
            issue.issue_card("OP-1", "issue-1")
        self.fake.get_roles = lambda _user: ["School Admin"]
        self.operation.customer = "OTHER"
        with self.assertRaisesRegex(ValueError, "family/customer/product"):
            issue.issue_card("OP-1", "issue-1")


    def test_issue_entry_failure_rolls_back_its_savepoint(self):
        original = self.fake.get_doc.side_effect
        def failing_get_doc(dt, name=None, **kwargs):
            if isinstance(dt, dict) and dt.get("doctype") == "QAS PAYG Entry":
                raise RuntimeError("entry failed")
            return original(dt, name, **kwargs)
        self.fake.get_doc.side_effect = failing_get_doc
        with self.assertRaisesRegex(RuntimeError, "entry failed"):
            issue.issue_card("OP-1", "issue-1")
        self.fake.db.rollback.assert_called_once()
        self.assertEqual(self.fake.db.rollback.call_args.kwargs.keys(), {"save_point"})

    def test_existing_purchase_operation_reuses_its_key(self):
        self.fake.db.get_value.side_effect = lambda dt, key, field: "OP-1" if dt == "QAS PAYG Operation" else self.value(dt, key, field)
        self.operation.request_key = "purchase-1"
        self.assertIs(issue.create_or_get_purchase_operation("P-1", "PROD-1", "purchase-1"), self.operation)
        with self.assertRaisesRegex(ValueError, "another purchase"):
            issue.create_or_get_purchase_operation("P-2", "PROD-1", "purchase-1")
