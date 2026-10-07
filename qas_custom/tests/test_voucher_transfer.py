from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services import voucher_transfer as service


class Doc(frappe._dict):
    def save(self, **kwargs):
        self.saved = True
    def add_comment(self, *args):
        self.comment = args


class TestVoucherTransfer(TestCase):
    def setUp(self):
        self.source = Doc(name="Storytelling", term_session_fee="40.00", status="Active", is_makeup_course=0)
        self.target = Doc(name="Designer", term_session_fee=40, status="Active", is_makeup_course=0)
        self.vouchers = {name: Doc(name=name, student="Student", course="Storytelling", status="Valid", expiry_date="2027-09-01", issue_date="2026-09-01", original_session="Original", leave_request="Leave") for name in ["MV-1", "MV-2"]}
        self.db = Mock()
        self.patches = [
            patch.object(service, "_", side_effect=lambda text: text),
            patch.object(service, "_require_school_admin"),
            patch.object(service, "_school_admin_required_reason", side_effect=lambda reason: reason if reason else frappe.throw("Reason required")),
            patch.object(service, "_school_admin_makeup_lock", side_effect=lambda _: nullcontext()),
            patch.object(service, "_get_school_admin_voucher_family_context", return_value=(Doc(name="Parent"), [{"name": "Student"}], None)),
            patch.object(service, "get_makeup_difference_invoice", return_value=None),
            patch.object(service, "sync_makeup_voucher_label"),
            patch.object(service, "today", return_value="2026-10-07"),
            patch.object(service.frappe, "db", self.db),
            patch.object(service.frappe, "throw", side_effect=ValueError),
            patch.object(service.frappe, "get_doc", side_effect=lambda doctype, name, **kw: {"Storytelling": self.source, "Designer": self.target}[name] if doctype == "Course" else self.vouchers[name]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def transfer(self, **overrides):
        values = dict(parent="Parent", voucher_ids=["MV-2", "MV-1"], source_course="Storytelling", target_course="Designer", reason="Parent request")
        values.update(overrides)
        return service.transfer_vouchers(**values)

    def test_equal_price_batch_preserves_identity_and_leave(self):
        result = self.transfer()
        self.assertEqual(result["transferred"], 2)
        for doc in self.vouchers.values():
            self.assertEqual(doc.course, "Designer")
            self.assertEqual(doc.expiry_date, "2027-09-01")
            self.assertEqual(doc.original_session, "Original")
            self.assertEqual(doc.leave_request, "Leave")
            self.assertIn("Storytelling → Designer", doc.comment[1])
        self.assertTrue(all(call.kwargs.get("for_update") for call in service.frappe.get_doc.call_args_list))
        self.db.rollback.assert_not_called()

    def test_transfer_back(self):
        self.transfer()
        result = self.transfer(source_course="Designer", target_course="Storytelling")
        self.assertEqual(result["transferred"], 2)
        self.assertEqual(self.vouchers["MV-1"].course, "Storytelling")

    def test_cheaper_and_more_expensive_targets_rejected(self):
        for fee in (35, 45):
            self.target.term_session_fee = fee
            with self.assertRaises(ValueError): self.transfer()
        self.assertFalse(any(v.get("saved") for v in self.vouchers.values()))

    def test_missing_zero_and_invalid_prices_rejected(self):
        for value in (None, 0, -1, "invalid", "NaN", "Infinity"):
            self.source.term_session_fee = value
            with self.assertRaises(ValueError): self.transfer()

    def test_inactive_and_dedicated_targets_rejected(self):
        self.target.status = "Inactive"
        with self.assertRaises(ValueError): self.transfer()
        self.target.status = "Active"; self.target.is_makeup_course = 1
        with self.assertRaises(ValueError): self.transfer()

    def test_unavailable_voucher_prevents_entire_batch(self):
        for field, value in [("status", "Used"), ("expiry_date", "2026-10-06"), ("used_on_session", "Session"), ("used_by_student", "Student")]:
            original = self.vouchers["MV-2"].get(field)
            self.vouchers["MV-2"][field] = value
            with self.assertRaises(ValueError): self.transfer()
            self.assertFalse(any(v.get("saved") for v in self.vouchers.values()))
            self.vouchers["MV-2"][field] = original
        self.db.rollback.assert_called_with(save_point="voucher_transfer")

    def test_course_changed_since_preview_rejected(self):
        self.vouchers["MV-2"].course = "Other"
        with self.assertRaises(ValueError): self.transfer()
        self.assertFalse(self.vouchers["MV-1"].get("saved"))

    def test_wrong_family_rejected(self):
        service._get_school_admin_voucher_family_context.return_value = (Doc(name="Parent"), [Doc(name="Other Student")], None)
        with self.assertRaises(ValueError): self.transfer()
        self.assertFalse(self.vouchers["MV-1"].get("saved"))

    def test_active_difference_invoice_rejected(self):
        service.get_makeup_difference_invoice.return_value = Doc(name="Invoice", docstatus=0)
        with self.assertRaises(ValueError): self.transfer()

    def test_write_failure_rolls_back(self):
        with patch.object(Doc, "save", side_effect=[None, RuntimeError("Write failed")]):
            with self.assertRaises(RuntimeError): self.transfer()
        self.db.rollback.assert_called_once_with(save_point="voucher_transfer")

    def test_invalid_request_and_missing_reason_rejected(self):
        for values in ({"voucher_ids": []}, {"voucher_ids": "{}"}, {"voucher_ids": "invalid-json"}, {"voucher_ids": [1]}, {"source_course": ""}, {"reason": ""}):
            with self.assertRaises(ValueError): self.transfer(**values)

    def test_permission_rechecked(self):
        service._require_school_admin.side_effect = ValueError("Denied")
        with self.assertRaises(ValueError): self.transfer()
        self.db.savepoint.assert_not_called()

    def test_options_exclude_unavailable_and_missing_price(self):
        self.vouchers["MV-2"].status = "Used"
        with patch.object(service, "_get_school_admin_family_context", return_value=(Doc(name="Parent"), [{"name": "Student"}])), patch.object(service.frappe, "get_all", side_effect=[list(self.vouchers.values()), [self.source, self.target, Doc(name="NoPrice", term_session_fee=0)]]):
            result = service.get_transfer_options(parent="Parent")
        self.assertEqual([v.name for v in result["vouchers"]], ["MV-1"])
        self.assertEqual([c["name"] for c in result["courses"]], ["Storytelling", "Designer"])
