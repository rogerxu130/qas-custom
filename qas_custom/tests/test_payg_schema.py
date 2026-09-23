"""Site-free PAYG schema and migration contract tests."""

import ast
import json
import runpy
import sys
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
DOCTYPES = ROOT / "qas_custom" / "doctype"


def definition(name):
    slug = name.lower().replace(" ", "_")
    return json.loads((DOCTYPES / slug / f"{slug}.json").read_text())


def fields(name):
    return {field["fieldname"]: field for field in definition(name)["fields"]}


class TestPaygSchema(TestCase):
    def test_independent_doctypes_and_permissions(self):
        for name in ("QAS PAYG Product", "QAS PAYG Card", "QAS PAYG Entry",
                     "QAS PAYG Booking", "QAS PAYG Operation"):
            doc = definition(name)
            self.assertFalse(doc.get("istable"), name)
            self.assertNotIn("parent", fields(name), name)
            self.assertEqual({p["role"] for p in doc["permissions"]},
                             {"System Manager", "School Admin"})
            self.assertTrue((DOCTYPES / name.lower().replace(" ", "_") / "__init__.py").exists())

    def test_product_card_and_transfer_shape(self):
        product = fields("QAS PAYG Product")
        self.assertEqual(product["course"]["options"], "Course")
        self.assertEqual(product["course"]["unique"], 1)
        self.assertEqual(product["sessions_per_card"]["default"], "10")
        self.assertEqual(product["sessions_per_card"]["read_only"], 1)
        self.assertEqual(product["invoice_item"]["options"], "Item")
        card = fields("QAS PAYG Card")
        for field, target in {"family_parent": "Parent", "customer": "Customer",
                              "product": "QAS PAYG Product", "course": "Course"}.items():
            self.assertEqual(card[field]["options"], target)
        for field in ("issued_on", "expires_on", "unit_price_snapshot",
                      "available_count", "reserved_count", "consumed_count"):
            self.assertIn(field, card)
        self.assertEqual(card["status"]["options"].split("\n"),
                         ["Active", "Paused", "Transferred"])

    def test_entry_booking_operation_history_and_keys(self):
        entry = fields("QAS PAYG Entry")
        self.assertTrue({"Issue", "Reserve", "Consume", "Return", "Correction",
                         "Transfer Out", "Transfer In"}.issubset(set(entry["kind"]["options"].split("\n"))))
        for field in ("card", "booking", "operation", "available_delta", "reserved_delta",
                      "consumed_delta", "actor", "occurred_at", "reason"):
            self.assertIn(field, entry)
        self.assertEqual(entry["operation_key"]["unique"], 1)
        booking = fields("QAS PAYG Booking")
        for field in ("family_parent", "student", "card", "course_session",
                      "attendance_entry", "status", "cancellable_until", "cancelled_at",
                      "cancel_reason", "cancelled_by", "course_snapshot", "card_expires_on_snapshot"):
            self.assertIn(field, booking)
        self.assertEqual(booking["request_key"]["unique"], 1)
        operation = fields("QAS PAYG Operation")
        for field in ("operation_type", "request_key", "issue_request_key",
                      "invoice_request_key", "source_card", "target_card", "card",
                      "family_parent", "old_course", "new_course", "old_expiry",
                      "new_expiry", "old_price", "new_price", "quantity", "price_delta",
                      "invoice", "actor", "reason", "status", "created_at"):
            self.assertIn(field, operation)

    def test_controllers_and_patch_contract(self):
        for slug in ("product", "card", "entry", "booking", "operation"):
            path = DOCTYPES / f"qas_payg_{slug}" / f"qas_payg_{slug}.py"
            tree = ast.parse(path.read_text())
            controller = next(node for node in tree.body if isinstance(node, ast.ClassDef))
            self.assertIn("validate", {node.name for node in controller.body
                                       if isinstance(node, ast.FunctionDef)})
        patch = (ROOT / "patches" / "v2026_09_23_payg_indexes.py").read_text()
        ast.parse(patch)
        for name in ("idx_payg_card_lookup", "idx_payg_booking_card",
                     "idx_payg_booking_student", "idx_payg_entry_card_creation",
                     "idx_payg_operation_type_request_unique", "idx_payg_issue_key_unique",
                     "idx_payg_invoice_key_unique"):
            self.assertIn(name, patch)
        patches = (ROOT / "patches.txt").read_text()
        self.assertIn("[post_model_sync]", patches)
        self.assertIn("qas_custom.patches.v2026_09_23_payg_indexes", patches.split("[post_model_sync]")[1])


class TestPaygControllers(TestCase):
    def setUp(self):
        self.frappe = types.ModuleType("frappe")
        self.frappe.db = Mock()
        self.frappe.throw = Mock(side_effect=ValueError)
        model = types.ModuleType("frappe.model")
        document = types.ModuleType("frappe.model.document")
        document.Document = type("Document", (), {
            "is_new": lambda self: True,
            "get": lambda self, field: getattr(self, field, None),
            "__getattr__": lambda self, field: None,
        })
        self.modules = patch.dict(sys.modules, {"frappe": self.frappe,
                                                "frappe.model": model,
                                                "frappe.model.document": document})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        helper_path = DOCTYPES / "payg_validation.py"
        helper = types.ModuleType("qas_custom.qas_custom.doctype.payg_validation")
        exec(compile(helper_path.read_text(), str(helper_path), "exec"), helper.__dict__)
        self.helper_module = patch.dict(sys.modules, {helper.__name__: helper})
        self.helper_module.start()
        self.addCleanup(self.helper_module.stop)

    def controller(self, short):
        slug = f"qas_payg_{short}"
        path = DOCTYPES / slug / f"{slug}.py"
        namespace = runpy.run_path(str(path))
        return namespace["QASPAYG" + short.title()]

    @staticmethod
    def doc(**values):
        class Doc(types.SimpleNamespace):
            def __getattr__(self, name):
                return None
        obj = Doc(**values)
        obj.get = lambda field: getattr(obj, field, None)
        obj.is_new = lambda: values.get("new", True)
        return obj

    def test_product_fixes_ten_and_rejects_negative_price(self):
        controller = self.controller("product")
        controller.validate(self.doc(sessions_per_card=10, standard_card_price=100))
        with self.assertRaises(ValueError):
            controller.validate(self.doc(sessions_per_card=9, standard_card_price=100))
        with self.assertRaises(ValueError):
            controller.validate(self.doc(sessions_per_card=10, standard_card_price=-1))

    def test_transfer_target_card_can_hold_fewer_than_ten(self):
        controller = self.controller("card")
        self.frappe.db.get_value.side_effect = ["CUS-1", "COURSE-2"]
        doc = self.doc(family_parent="P-1", customer="CUS-1", product="PROD-2",
                       course="COURSE-2", issued_on="2026-09-23", expires_on="2027-03-23",
                       unit_price_snapshot=42, available_count=7, reserved_count=0,
                       consumed_count=0, status="Active", new=False, name="CARD-2")
        doc.get_doc_before_save = lambda: self.doc(**{k: doc.get(k) for k in (
            "family_parent", "customer", "product", "course", "issued_on",
            "unit_price_snapshot", "status")})
        self.frappe.db.sql.return_value = [(7, 0, 0)]
        controller.validate(doc)
        doc.available_count = -1
        with self.assertRaises(ValueError):
            controller.validate(doc)

    def test_new_card_cannot_start_with_unledgered_sessions(self):
        controller = self.controller("card")
        self.frappe.db.get_value.side_effect = ["CUS-1", "COURSE-1"]
        doc = self.doc(family_parent="P-1", customer="CUS-1", product="PROD-1",
                       course="COURSE-1", issued_on="2026-09-23", expires_on="2027-03-23",
                       unit_price_snapshot=10, available_count=10, reserved_count=0,
                       consumed_count=0)
        with self.assertRaises(ValueError):
            controller.validate(doc)

    def test_entry_is_append_only_and_transfer_in_accepts_partial_card(self):
        controller = self.controller("entry")
        self.frappe.get_doc = Mock(return_value=self.doc(family_parent="P-1", available_count=0,
                                                         reserved_count=0, consumed_count=0))
        self.frappe.get_doc.side_effect = lambda dt, name, for_update=False: (
            self.doc(family_parent="P-1", operation_type="Exchange", target_card="CARD-2")
            if dt == "QAS PAYG Operation" else
            self.doc(family_parent="P-1", available_count=0, reserved_count=0, consumed_count=0))
        entry = self.doc(booking=None, operation="OP-1", kind="Transfer In", available_delta=7,
                         reserved_delta=0, consumed_delta=0, card="CARD-2")
        controller.validate(entry)
        controller.after_insert(entry)
        self.assertEqual(self.frappe.get_doc.call_args_list[-1].args,
                         ("QAS PAYG Card", "CARD-2"))
        self.frappe.db.set_value.assert_called_once_with(
            "QAS PAYG Card", "CARD-2",
            {"available_count": 7, "reserved_count": 0, "consumed_count": 0},
            update_modified=False)
        entry.new = False
        entry.is_new = lambda: False
        with self.assertRaises(ValueError):
            controller.validate(entry)
        with self.assertRaises(ValueError):
            controller.on_trash(entry)

    def test_reservation_entry_shapes(self):
        controller = self.controller("entry")
        self.frappe.get_doc = Mock(side_effect=lambda dt, name, for_update=False: (
            self.doc(card="CARD-1") if dt == "QAS PAYG Booking" else
            self.doc(family_parent="P-1", available_count=2, reserved_count=1, consumed_count=1)))
        for kind, deltas in (("Reserve", (-1, 1, 0)), ("Consume", (0, -1, 1)),
                             ("Return", (1, -1, 0)), ("Return", (1, 0, -1))):
            self.frappe.db.sql.return_value = (
                [] if kind == "Reserve" else [self.doc(kind="Reserve")]
                if deltas != (1, 0, -1) else [self.doc(kind="Reserve"), self.doc(kind="Consume")])
            controller.validate(self.doc(booking="BOOK-1", card="CARD-1", kind=kind, available_delta=deltas[0],
                                         reserved_delta=deltas[1], consumed_delta=deltas[2]))
        self.frappe.db.sql.return_value = []
        with self.assertRaises(ValueError):
            controller.validate(self.doc(booking="BOOK-1", card="CARD-1", kind="Reserve", available_delta=-2,
                                         reserved_delta=2, consumed_delta=0))

    def test_entry_cannot_overdraw_card_or_use_other_family_operation(self):
        controller = self.controller("entry")
        operation = self.doc(operation_type="Exchange", family_parent="P-1",
                             source_card="CARD-1", target_card="CARD-2")
        card = self.doc(family_parent="P-1", available_count=0,
                        reserved_count=0, consumed_count=0)
        self.frappe.get_doc = Mock(side_effect=lambda dt, name, for_update=False: (
            operation if dt == "QAS PAYG Operation" else card))
        entry = self.doc(booking=None, operation="OP-1", card="CARD-1", kind="Transfer Out",
                         available_delta=-1, reserved_delta=0, consumed_delta=0)
        with self.assertRaises(ValueError):
            controller.validate(entry)
        operation.family_parent = "P-2"
        with self.assertRaises(ValueError):
            controller.validate(entry)

    def test_repeated_consume_and_return_cannot_use_other_bookings_balance(self):
        controller = self.controller("entry")
        booking = self.doc(card="CARD-1")
        card = self.doc(family_parent="P-1", available_count=8, reserved_count=2,
                        consumed_count=1)
        self.frappe.get_doc = Mock(side_effect=lambda dt, name, for_update=False: {
            "QAS PAYG Booking": booking, "QAS PAYG Card": card}[dt])
        self.frappe.db.sql.return_value = [self.doc(kind="Reserve"), self.doc(kind="Consume")]
        entry = self.doc(booking="BOOK-1", operation=None, card="CARD-1", kind="Consume",
                         available_delta=0, reserved_delta=-1, consumed_delta=1)
        with self.assertRaises(ValueError):
            controller.validate(entry)
        entry.kind = "Return"
        entry.available_delta, entry.reserved_delta, entry.consumed_delta = 1, -1, 0
        with self.assertRaises(ValueError):
            controller.validate(entry)

    def test_entry_lock_order_is_booking_then_card_then_history(self):
        controller = self.controller("entry")
        calls = []
        booking = self.doc(card="CARD-1")
        card = self.doc(family_parent="P-1", available_count=1, reserved_count=0,
                        consumed_count=0)
        def get_doc(doctype, name, for_update=False):
            calls.append((doctype, for_update))
            return booking if doctype == "QAS PAYG Booking" else card
        self.frappe.get_doc = Mock(side_effect=get_doc)
        self.frappe.db.sql.side_effect = lambda *args, **kwargs: calls.append(("history", "for update")) or []
        entry = self.doc(booking="BOOK-1", operation=None, card="CARD-1", kind="Reserve",
                         available_delta=-1, reserved_delta=1, consumed_delta=0)
        controller.validate(entry)
        self.assertEqual(calls, [("QAS PAYG Booking", True), ("QAS PAYG Card", True),
                                 ("history", "for update")])
        self.frappe.db.get_value.assert_not_called()

    def test_booking_rejects_cross_family(self):
        controller = self.controller("booking")
        self.frappe.db.get_value.return_value = types.SimpleNamespace(
            family_parent="P-1", course="C-1", expires_on="2027-03-23")
        booking = self.doc(card="CARD-1", family_parent="P-2", course_snapshot="C-1",
                           card_expires_on_snapshot="2027-03-23")
        with self.assertRaises(ValueError):
            controller.validate(booking)

    def test_new_booking_requires_locked_service_context_and_no_post_series_reads(self):
        controller = self.controller("booking")
        booking = self.doc(card="CARD-1", family_parent="P-1", student="S-1",
                           course_session="SESSION-1", course_snapshot="C-1",
                           card_expires_on_snapshot="2027-03-23", attendance_entry=None,
                           request_key="req-1", cancellable_until="2026-10-01",
                           status="Reserved")
        with self.assertRaises(ValueError):
            controller.validate(booking)
        self.frappe.db.get_value.assert_not_called()
        booking.flags = {"payg_create_context": {
            "token": controller._SERVICE_CREATE_TOKEN,
            "family_parent": "P-1", "student": "S-1", "card": "CARD-1",
            "card_family": "P-1", "card_course": "C-1", "course_session": "SESSION-1",
            "course_snapshot": "C-1", "card_expires_on_snapshot": "2027-03-23",
            "request_key": "req-1", "cancellable_until": "2026-10-01"}}
        controller.validate(booking)
        self.frappe.db.get_value.assert_not_called()
        booking.flags["payg_create_context"]["card_course"] = "OTHER"
        with self.assertRaises(ValueError):
            controller.validate(booking)

    def test_existing_booking_requires_service_mutation_token_without_relation_locks(self):
        controller = self.controller("booking")
        booking = self.doc(card="CARD-1", family_parent="P-1", student="S-1",
                           course_session="SESSION-1", course_snapshot="C-1",
                           card_expires_on_snapshot="2027-03-23", attendance_entry=None,
                           status="Reserved")
        booking.is_new = lambda: False
        booking.get_doc_before_save = lambda: booking
        with self.assertRaises(ValueError):
            controller.validate(booking)
        booking.flags = {"payg_mutation_token": controller._SERVICE_MUTATION_TOKEN}
        controller.validate(booking)
        self.frappe.db.get_value.assert_not_called()
        self.frappe.db.sql.assert_not_called()

    def test_booking_identity_cannot_change_after_insert(self):
        controller = self.controller("booking")
        before = self.doc(family_parent="P-1", student="S-1", card="CARD-1",
                          course_session="SESSION-1", course_snapshot="C-1",
                          card_expires_on_snapshot="2027-03-23", request_key="req-1")
        self.frappe.db.get_value.side_effect = [
            "P-1", types.SimpleNamespace(family_parent="P-1", course="C-1", expires_on="2027-03-23")]
        booking = self.doc(family_parent="P-1", student="S-1", card="CARD-1",
                           course_session="SESSION-2", course_snapshot="C-1",
                           card_expires_on_snapshot="2027-03-23", request_key="req-1",
                           attendance_entry=None, new=False)
        booking.get_doc_before_save = lambda: before
        booking.flags = {"payg_mutation_token": controller._SERVICE_MUTATION_TOKEN}
        with self.assertRaises(ValueError):
            controller.validate(booking)

    def test_booking_cannot_cancel_without_return_entry(self):
        controller = self.controller("booking")
        before = self.doc(family_parent="P-1", student="S-1", card="CARD-1",
                          course_session="SESSION-1", course_snapshot="C-1",
                          card_expires_on_snapshot="2027-03-23", cancellable_until="2026-10-01",
                          request_key="req-1", status="Reserved", attendance_entry=None)
        booking = self.doc(**{**before.__dict__, "new": False, "status": "Cancelled",
                              "cancelled_at": "2026-09-23", "cancelled_by": "ADMIN",
                              "cancel_reason": "Closure"})
        booking.get_doc_before_save = lambda: before
        booking.flags = {"payg_mutation_token": controller._SERVICE_MUTATION_TOKEN}
        self.frappe.db.exists.return_value = False
        with self.assertRaises(ValueError):
            controller.validate(booking)
        self.frappe.db.exists.assert_called_once_with(
            "QAS PAYG Entry", {"booking": booking.name, "kind": "Return"})

    def test_operation_action_key_cannot_be_replaced(self):
        controller = self.controller("operation")
        before = self.doc(operation_type="Purchase", request_key="req-1", family_parent="P-1",
                          customer="CUS-1", product="PROD-1", source_card=None,
                          target_card=None, card=None, issue_request_key="issue-1",
                          invoice_request_key=None, invoice=None, status="Pending")
        op = controller()
        op.__dict__.update(before.__dict__)
        op.issue_request_key = "issue-2"
        op.get = lambda field: getattr(op, field, None)
        op.is_new = lambda: False
        op.get_doc_before_save = lambda: before
        self.frappe.db.get_value.side_effect = lambda dt, name, field, **kw: (
            "CUS-1" if dt == "Parent" else "C-1")
        with self.assertRaises(ValueError):
            op.validate()

    def test_operation_reason_cannot_change_or_clear_after_insert(self):
        controller = self.controller("operation")
        before = self.doc(operation_type="Purchase", request_key="req-1",
                          family_parent="P-1", customer="CUS-1", product="PROD-1",
                          reason="Approved correction", status="Completed")
        op = controller()
        op.__dict__.update(before.__dict__)
        op.get = lambda field: getattr(op, field, None)
        op.is_new = lambda: False
        op.get_doc_before_save = lambda: before
        op._validate_purchase = lambda cards, family_customer: None
        self.frappe.db.get_value.return_value = "CUS-1"
        op.validate()
        for changed_reason in ("Revised reason", None):
            op.reason = changed_reason
            with self.assertRaises(ValueError):
                op.validate()

    def test_operation_audit_survives_currency_and_datetime_db_reload(self):
        controller = self.controller("operation")
        before = self.doc(operation_type="Exchange", request_key="exchange-1",
                          family_parent="P-1", customer="CUS-1", product="PROD-2",
                          source_card="CARD-1", target_card=None, card=None,
                          old_course="C-1", new_course="C-2", old_price=40.0,
                          new_price=55.0, quantity=7, price_delta=105.0,
                          actor="admin", created_at=datetime(2026, 9, 23, 12),
                          status="Pending")
        op = controller()
        op.__dict__.update(before.__dict__)
        op.old_price, op.new_price, op.price_delta = (Decimal("40"), Decimal("55"), Decimal("105"))
        op.created_at = datetime(2026, 9, 23, 2, tzinfo=timezone.utc)
        op.target_card, op.status = "CARD-2", "Completed"
        op.get = lambda field: getattr(op, field, None)
        op.is_new = lambda: False
        op.get_doc_before_save = lambda: before
        controller._validate_existing(op)
        op.price_delta = Decimal("106")
        with self.assertRaises(ValueError):
            controller._validate_existing(op)
        self.assertIn("price_delta", self.frappe.throw.call_args.args[0])
        op.price_delta = Decimal("105")
        op.created_at = datetime(2026, 9, 23, 3, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            controller._validate_existing(op)
        self.assertIn("created_at", self.frappe.throw.call_args.args[0])

    def test_operation_currency_audit_uses_persisted_nine_decimal_precision(self):
        controller = self.controller("operation")
        before = self.doc(old_price=0.1, new_price=0.100000001, price_delta=-0.000000001,
                          status="Pending")
        op = controller()
        op.__dict__.update(before.__dict__)
        op.old_price = Decimal("0.100000000")
        op.new_price = Decimal("0.1000000009")  # Persists as 0.100000001.
        op.price_delta = Decimal("-0.000000001")
        op.get = lambda field: getattr(op, field, None)
        op.is_new = lambda: False
        op.get_doc_before_save = lambda: before
        controller._validate_existing(op)
        op.new_price = Decimal("0.1000000015")  # Persists as 0.100000002.
        with self.assertRaises(ValueError):
            controller._validate_existing(op)
        self.assertIn("new_price", self.frappe.throw.call_args.args[0])

    def test_card_identity_and_transferred_status_are_final(self):
        controller = self.controller("card")
        before = self.doc(family_parent="P-1", customer="CUS-1", product="PROD-1",
                          course="C-1", issued_on="2026-09-23", unit_price_snapshot=10,
                          status="Transferred")
        card = self.doc(**{**before.__dict__, "new": False, "name": "CARD-1",
                           "expires_on": "2027-03-23", "available_count": 0,
                           "reserved_count": 0, "consumed_count": 0,
                           "status": "Active"})
        card.get_doc_before_save = lambda: before
        self.frappe.db.get_value.side_effect = ["CUS-1", "C-1"]
        self.frappe.db.sql.return_value = [(0, 0, 0)]
        with self.assertRaises(ValueError):
            controller.validate(card)

    def test_booking_checks_attendance_source(self):
        controller = self.controller("booking")
        self.frappe.db.get_value.side_effect = [
            types.SimpleNamespace(family_parent="P-1", course="C-1", expires_on="2027-03-23"),
            "P-1", "WTS-1", "C-1",
            types.SimpleNamespace(student="S-1", course_session="SESSION-1",
                                  source_doctype="Adhoc Booking", source_document="BOOK-1")]
        booking = self.doc(name="BOOK-1", family_parent="P-1", student="S-1",
                           card="CARD-1", course_session="SESSION-1", course_snapshot="C-1",
                           card_expires_on_snapshot="2027-03-23", attendance_entry="ATT-1")
        with self.assertRaises(ValueError):
            controller.validate(booking)

    def test_completed_exchange_requires_target(self):
        controller = self.controller("operation")
        self.frappe.db.get_value.side_effect = lambda dt, name, field, **kw: (
            "CUS-1" if dt == "Parent" else "C-2" if dt == "QAS PAYG Product" else types.SimpleNamespace(
                family_parent="P-1", customer="CUS-1", product="PROD-1", course="C-1"))
        op = controller()
        op.__dict__.update(operation_type="Exchange", status="Completed", family_parent="P-1",
                      customer="CUS-1", product="PROD-2", source_card="CARD-1",
                      target_card=None, card=None, old_course="C-1", new_course="C-2",
                      old_price=40, new_price=45, quantity=7)
        op.get = lambda field: getattr(op, field, None)
        with self.assertRaises(ValueError):
            op.validate()

    def test_purchase_pending_allows_card_and_invoice_later(self):
        controller = self.controller("operation")
        self.frappe.db.get_value.side_effect = lambda dt, name, field, **kw: (
            "CUS-1" if dt == "Parent" else "C-1")
        op = controller()
        op.__dict__.update(operation_type="Purchase", status="Pending", family_parent="P-1",
                           customer="CUS-1", product="PROD-1", source_card=None,
                           target_card=None, card=None, old_price=None, new_price=None,
                           quantity=0, invoice=None)
        op.get = lambda field: getattr(op, field, None)
        op.validate()

    def test_exchange_pending_allows_target_later(self):
        controller = self.controller("operation")
        self.frappe.db.get_value.side_effect = lambda dt, name, field, **kw: (
            "CUS-1" if dt == "Parent" else "C-2" if dt == "QAS PAYG Product" else
            types.SimpleNamespace(family_parent="P-1", customer="CUS-1",
                                  product="PROD-1", course="C-1"))
        op = controller()
        op.__dict__.update(operation_type="Exchange", status="Pending", family_parent="P-1",
                           customer="CUS-1", product="PROD-2", source_card="CARD-1",
                           target_card=None, card=None, old_course="C-1", new_course="C-2",
                           old_price=40, new_price=45, quantity=7)
        op.get = lambda field: getattr(op, field, None)
        op.validate()

    def test_index_patch_is_idempotent_and_checks_definitions(self):
        patch_file = ROOT / "patches" / "v2026_09_23_payg_indexes.py"
        namespace = runpy.run_path(str(patch_file))
        ensure = namespace["_ensure_index"]
        self.frappe.db.sql.return_value = [{"Key_name": "idx_payg_operation_type_request_unique",
                                            "Seq_in_index": 1, "Column_name": "operation_type", "Non_unique": 0},
                                           {"Key_name": "idx_payg_operation_type_request_unique",
                                            "Seq_in_index": 2, "Column_name": "request_key", "Non_unique": 0}]
        ensure("QAS PAYG Operation", "idx_payg_operation_type_request_unique",
               ("operation_type", "request_key"), True)
        self.frappe.db.sql.assert_called_once()
        with self.assertRaises(ValueError):
            ensure("QAS PAYG Operation", "idx_payg_operation_type_request_unique",
                   ("request_key", "operation_type"), True)

    def test_index_patch_creates_composite_unique_key(self):
        namespace = runpy.run_path(str(ROOT / "patches" / "v2026_09_23_payg_indexes.py"))
        self.frappe.db.sql.return_value = []
        namespace["_ensure_index"]("QAS PAYG Operation", "idx_payg_operation_type_request_unique",
                                   ("operation_type", "request_key"), True)
        sql = self.frappe.db.sql.call_args_list[-1].args[0]
        self.assertIn("add unique index", sql)
        self.assertIn("`operation_type`, `request_key`", sql)
