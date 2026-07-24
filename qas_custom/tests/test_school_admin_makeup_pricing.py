from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.makeup.commands import _course_accepts_makeup_voucher
from qas_custom.modules.makeup.pricing import (
	classify_difference_invoice,
	classify_makeup_target,
)
from qas_custom.services.school_admin import _apply_course_pricing_defaults
from qas_custom.services.school_admin import _ensure_makeup_price_difference_invoice


class FakeCourse:
	doctype = "Course"

	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def save(self, ignore_permissions=False):
		self.saved = ignore_permissions


class TestSchoolAdminMakeupPricing(TestCase):
	def test_same_course_requires_no_price_adjustment(self):
		result = classify_makeup_target(
			source_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
			target_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
			accepted_courses=[],
		)

		self.assertEqual(result["classification"], "same_course")
		self.assertEqual(result["price_difference"], 0)
		self.assertFalse(result["requires_difference_invoice"])

	def test_more_expensive_ordinary_course_creates_difference(self):
		result = classify_makeup_target(
			source_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
			target_course={"name": "Design", "term_session_fee": 75, "is_makeup_course": 0},
			accepted_courses=[],
		)

		self.assertEqual(result["classification"], "ordinary_cross_course")
		self.assertEqual(result["source_session_fee"], 68)
		self.assertEqual(result["target_session_fee"], 75)
		self.assertEqual(result["price_difference"], 7)
		self.assertTrue(result["requires_difference_invoice"])

	def test_same_or_cheaper_ordinary_course_does_not_refund(self):
		result = classify_makeup_target(
			source_course={"name": "Design", "term_session_fee": 75, "is_makeup_course": 0},
			target_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
			accepted_courses=[],
		)

		self.assertEqual(result["classification"], "ordinary_cross_course")
		self.assertEqual(result["price_difference"], 0)
		self.assertFalse(result["requires_difference_invoice"])

	def test_dedicated_makeup_course_ignores_prices_when_explicitly_accepted(self):
		result = classify_makeup_target(
			source_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
			target_course={"name": "Holiday Makeup", "term_session_fee": None, "is_makeup_course": 1},
			accepted_courses=["Anime"],
		)

		self.assertEqual(result["classification"], "dedicated_makeup_course")
		self.assertEqual(result["price_difference"], 0)
		self.assertFalse(result["requires_difference_invoice"])

	def test_dedicated_makeup_course_with_empty_acceptance_list_is_rejected(self):
		with self.assertRaisesRegex(ValueError, "does not accept"):
			classify_makeup_target(
				source_course={"name": "Anime", "term_session_fee": 68, "is_makeup_course": 0},
				target_course={"name": "Holiday Makeup", "term_session_fee": None, "is_makeup_course": 1},
				accepted_courses=[],
			)

	@patch("qas_custom.modules.makeup.commands.frappe.get_cached_doc")
	def test_parent_policy_does_not_treat_empty_makeup_acceptance_as_accept_all(self, mock_get_course):
		mock_get_course.return_value = SimpleNamespace(
			get=lambda key, default=None: {
				"is_makeup_course": 1,
				"accepted_makeup_course": [],
			}.get(key, default)
		)

		self.assertFalse(_course_accepts_makeup_voucher("Holiday Makeup", "Anime"))

	@patch("qas_custom.modules.makeup.commands.frappe.get_cached_doc")
	def test_only_school_admin_policy_allows_an_ordinary_cross_course_target(self, mock_get_course):
		mock_get_course.return_value = SimpleNamespace(
			get=lambda key, default=None: {"is_makeup_course": 0}.get(key, default)
		)

		self.assertFalse(_course_accepts_makeup_voucher("Designer", "Anime"))
		self.assertTrue(
			_course_accepts_makeup_voucher(
				"Designer",
				"Anime",
				allow_ordinary_cross_course=True,
			)
		)

	def test_draft_difference_invoice_is_auto_deleted_on_makeup_cancel(self):
		result = classify_difference_invoice(
			{"name": "SINV-DRAFT", "docstatus": 0, "status": "Draft", "paid_amount": 0, "outstanding_amount": 7}
		)
		self.assertEqual(result["action"], "delete_draft")
		self.assertFalse(result["upgrade_voucher_course"])

	def test_submitted_unpaid_difference_invoice_blocks_makeup_cancel(self):
		result = classify_difference_invoice(
			{"name": "SINV-UNPAID", "docstatus": 1, "status": "Unpaid", "paid_amount": 0, "outstanding_amount": 7}
		)
		self.assertEqual(result["action"], "block_unpaid")

	def test_partially_paid_difference_invoice_blocks_for_manual_review(self):
		result = classify_difference_invoice(
			{"name": "SINV-PARTIAL", "docstatus": 1, "status": "Partly Paid", "paid_amount": 3, "outstanding_amount": 4}
		)
		self.assertEqual(result["action"], "block_partial")

	def test_paid_difference_invoice_keeps_invoice_and_upgrades_voucher_course(self):
		result = classify_difference_invoice(
			{"name": "SINV-PAID", "docstatus": 1, "status": "Paid", "paid_amount": 7, "outstanding_amount": 0}
		)
		self.assertEqual(result["action"], "keep_paid")
		self.assertTrue(result["upgrade_voucher_course"])

	def test_dedicated_makeup_course_does_not_require_pricing_fields(self):
		doc = FakeCourse(
			is_makeup_course=1,
			full_term_fee=None,
			total_session_per_term=None,
			term_session_fee=None,
		)
		with patch("qas_custom.services.school_admin._has_field", return_value=True):
			_apply_course_pricing_defaults(doc)

		self.assertIsNone(doc.term_session_fee)

	def test_ordinary_course_still_requires_pricing_fields(self):
		doc = FakeCourse(
			is_makeup_course=0,
			full_term_fee=None,
			total_session_per_term=None,
			term_session_fee=None,
		)
		fake_frappe = SimpleNamespace(throw=Mock(side_effect=RuntimeError("Full term fee is required")))
		with patch("qas_custom.services.school_admin._has_field", return_value=True), patch(
			"qas_custom.services.school_admin.frappe",
			fake_frappe,
		):
			with self.assertRaisesRegex(RuntimeError, "Full term fee"):
				_apply_course_pricing_defaults(doc)

	@patch("qas_custom.services.school_admin._add_comment")
	@patch("qas_custom.services.school_admin.get_course_session_snapshot_label", return_value="Designer Art - 25 July")
	@patch("qas_custom.services.school_admin.get_student_parent_name", return_value="Ava")
	@patch("qas_custom.services.school_admin.get_invoice_item", return_value="Tuition Fee")
	@patch("qas_custom.services.school_admin.get_invoice_customer", return_value="CUS-001")
	@patch("qas_custom.services.school_admin._create_school_admin_manual_invoice_doc")
	@patch("qas_custom.services.school_admin.get_makeup_difference_invoice", return_value=None)
	def test_difference_invoice_is_draft_owned_by_family_and_linked_to_voucher(
		self,
		_mock_existing,
		mock_create_invoice,
		_mock_customer,
		_mock_item,
		_mock_student_name,
		_mock_session_label,
		_mock_comment,
	):
		invoice = FakeCourse(
			name="SINV-DRAFT",
			docstatus=0,
			status="Draft",
			grand_total=7,
			paid_amount=0,
			outstanding_amount=7,
		)
		mock_create_invoice.return_value = invoice
		voucher = FakeCourse(
			name="MV-001",
			price_difference_invoice=None,
			flags=SimpleNamespace(),
		)
		parent = FakeCourse(name="PAR-001")
		fake_frappe = SimpleNamespace(db=SimpleNamespace(has_column=Mock(return_value=True)))

		with patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = _ensure_makeup_price_difference_invoice(
				parent_doc=parent,
				voucher=voucher,
				student="STU-001",
				session={"session_id": "CS-001"},
				pricing={
					"requires_difference_invoice": True,
					"source_course": "Anime",
					"target_course": "Designer",
					"price_difference": 7,
				},
				reason="Parent requested a different course",
			)

		self.assertIs(result, invoice)
		payload = mock_create_invoice.call_args.args[0]
		self.assertEqual(payload["customer"], "CUS-001")
		self.assertEqual(payload["parent"], "PAR-001")
		self.assertEqual(payload["student"], "STU-001")
		self.assertEqual(payload["source_doctype"], "Makeup Voucher")
		self.assertEqual(payload["source_document"], "MV-001")
		self.assertEqual(payload["qas_is_manual_invoice"], 0)
		self.assertEqual(payload["items"][0]["rate"], 7)
		self.assertEqual(voucher.price_difference_invoice, "SINV-DRAFT")
		self.assertTrue(voucher.saved)
