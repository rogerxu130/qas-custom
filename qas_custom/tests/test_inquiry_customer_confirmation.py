from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.inquiry import (
	_get_campus_address,
	_should_reset_inquiry_confirmation,
	update_inquiry_confirmation_core,
)
from qas_custom.services.school_admin import (
	get_school_admin_inquiries_data,
	update_school_admin_inquiry_confirmation_data,
)


class FakeInquiry:
	def __init__(
		self,
		inquiry_type="Trial Lesson",
		status="Booked",
		course_session="SESSION-001",
		confirmation_status="Pending",
		contact_phone="0400000000",
		campus="South Brisbane",
	):
		self.name = "INQ-2026-00001"
		self.inquiry_type = inquiry_type
		self.status = status
		self.course_session = course_session
		self.confirmation_status = confirmation_status
		self.contact_phone = contact_phone
		self.campus = campus
		self.save = Mock()
		self.add_comment = Mock()

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestInquiryCustomerConfirmation(TestCase):
	def test_school_admin_can_confirm_matching_booked_trial(self):
		doc = FakeInquiry()
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe), patch(
			"qas_custom.services.inquiry.build_inquiry_detail",
			return_value={"inquiry": {"confirmation_status": "Customer Confirmed"}},
		):
			result = update_inquiry_confirmation_core(
				inquiry=doc.name,
				confirmation_status="Customer Confirmed",
				expected_course_session="SESSION-001",
				actor="school.admin@example.com",
			)

		self.assertEqual(doc.confirmation_status, "Customer Confirmed")
		doc.save.assert_called_once_with(ignore_permissions=True)
		doc.add_comment.assert_called_once()
		fake_frappe.db.commit.assert_called_once()
		self.assertEqual(result["inquiry"]["confirmation_status"], "Customer Confirmed")

	def test_school_admin_can_record_text_message_sent(self):
		doc = FakeInquiry()
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe), patch(
			"qas_custom.services.inquiry.build_inquiry_detail",
			return_value={"inquiry": {"confirmation_status": "Text Message Sent"}},
		):
			result = update_inquiry_confirmation_core(
				inquiry=doc.name,
				confirmation_status="Text Message Sent",
				expected_course_session="SESSION-001",
				actor="school.admin@example.com",
			)

		self.assertEqual(doc.confirmation_status, "Text Message Sent")
		doc.save.assert_called_once_with(ignore_permissions=True)
		doc.add_comment.assert_called_once()
		fake_frappe.db.commit.assert_called_once()
		self.assertEqual(result["inquiry"]["confirmation_status"], "Text Message Sent")

	def test_repeated_text_message_sent_is_idempotent(self):
		doc = FakeInquiry(confirmation_status="Text Message Sent")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe), patch(
			"qas_custom.services.inquiry.build_inquiry_detail",
			return_value={"inquiry": {"confirmation_status": "Text Message Sent"}},
		):
			update_inquiry_confirmation_core(
				inquiry=doc.name,
				confirmation_status="Text Message Sent",
				expected_course_session="SESSION-001",
			)

		doc.save.assert_not_called()
		doc.add_comment.assert_not_called()
		fake_frappe.db.commit.assert_not_called()

	def test_text_message_sent_does_not_downgrade_customer_confirmed(self):
		doc = FakeInquiry(confirmation_status="Customer Confirmed")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe), patch(
			"qas_custom.services.inquiry.build_inquiry_detail",
			return_value={"inquiry": {"confirmation_status": "Customer Confirmed"}},
		) as build_detail:
			result = update_inquiry_confirmation_core(
				inquiry=doc.name,
				confirmation_status="Text Message Sent",
				expected_course_session="SESSION-001",
			)

		self.assertEqual(result["inquiry"]["confirmation_status"], "Customer Confirmed")
		doc.save.assert_not_called()
		doc.add_comment.assert_not_called()
		fake_frappe.db.commit.assert_not_called()
		build_detail.assert_called_once_with(doc.name)

	def test_campus_address_is_resolved_from_booked_campus(self):
		with patch("qas_custom.services.inquiry.frappe.db.get_value", return_value="  10 Art Street  ") as get_value:
			address = _get_campus_address("South Brisbane")

		self.assertEqual(address, "10 Art Street")
		get_value.assert_called_once_with("Campus", "South Brisbane", "address")

	def test_missing_campus_has_no_address_lookup(self):
		with patch("qas_custom.services.inquiry.frappe.db.get_value") as get_value:
			self.assertEqual(_get_campus_address(""), "")

		get_value.assert_not_called()

	def test_text_message_sent_requires_campus_address(self):
		doc = FakeInquiry()
		fake_frappe = self._fake_frappe(doc)
		fake_frappe.db.get_value.return_value = ""
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Campus must have an address"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Text Message Sent",
					expected_course_session="SESSION-001",
				)

		doc.save.assert_not_called()

	def test_text_message_sent_requires_parent_phone(self):
		doc = FakeInquiry(contact_phone="")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "contact phone"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Text Message Sent",
					expected_course_session="SESSION-001",
				)

		doc.save.assert_not_called()

	def test_stale_course_session_is_rejected(self):
		doc = FakeInquiry(course_session="SESSION-002")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "session changed"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Customer Confirmed",
					expected_course_session="SESSION-001",
				)

		doc.save.assert_not_called()

	def test_terminal_inquiry_is_rejected(self):
		doc = FakeInquiry(status="Cancelled")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Booked or Rescheduled"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Customer Confirmed",
					expected_course_session="SESSION-001",
				)

	def test_school_visit_is_rejected(self):
		doc = FakeInquiry(inquiry_type="School Visit")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Trial Lesson"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Customer Confirmed",
					expected_course_session="SESSION-001",
				)

	def test_customer_confirmed_requires_course_session(self):
		doc = FakeInquiry(course_session="")
		fake_frappe = self._fake_frappe(doc)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Course Session"):
				update_inquiry_confirmation_core(
					inquiry=doc.name,
					confirmation_status="Customer Confirmed",
					expected_course_session="",
				)

	def test_course_session_change_resets_confirmation(self):
		doc = FakeInquiry(course_session="SESSION-002", confirmation_status="Customer Confirmed")
		self.assertTrue(_should_reset_inquiry_confirmation(doc, "SESSION-001"))

	def test_unrelated_save_keeps_customer_confirmed(self):
		doc = FakeInquiry(course_session="SESSION-001", confirmation_status="Customer Confirmed")
		self.assertFalse(_should_reset_inquiry_confirmation(doc, "SESSION-001"))

	def test_list_filter_is_applied_on_backend(self):
		fake_frappe = SimpleNamespace(get_all=Mock(return_value=[]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			get_school_admin_inquiries_data(confirmation_status="Customer Confirmed")

		filters = fake_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["confirmation_status"], "Customer Confirmed")

	def test_text_message_sent_list_filter_is_applied_on_backend(self):
		fake_frappe = SimpleNamespace(get_all=Mock(return_value=[]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			get_school_admin_inquiries_data(confirmation_status="Text Message Sent")

		filters = fake_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["confirmation_status"], "Text Message Sent")

	def test_school_admin_wrapper_checks_role_before_update(self):
		with patch(
			"qas_custom.services.school_admin._require_school_admin",
			side_effect=RuntimeError("School Admin only"),
		), patch("qas_custom.services.school_admin.update_inquiry_confirmation_core") as update_core:
			with self.assertRaisesRegex(RuntimeError, "School Admin only"):
				update_school_admin_inquiry_confirmation_data(
					inquiry="INQ-2026-00001",
					confirmation_status="Customer Confirmed",
					expected_course_session="SESSION-001",
				)

		update_core.assert_not_called()

	@staticmethod
	def _fake_frappe(doc):
		def throw(message, *_args, **_kwargs):
			raise RuntimeError(str(message))

		return SimpleNamespace(
			get_doc=Mock(return_value=doc),
			db=SimpleNamespace(commit=Mock(), get_value=Mock(return_value="10 Art Street")),
			session=SimpleNamespace(user="school.admin@example.com"),
			throw=throw,
		)
