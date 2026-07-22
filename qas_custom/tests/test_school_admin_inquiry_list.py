from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from frappe.utils import getdate

from qas_custom.services.school_admin import get_school_admin_inquiries_data


class TestSchoolAdminInquiryList(TestCase):
	def test_exact_status_query_and_type_share_the_same_backend_filter(self):
		fake_frappe = SimpleNamespace(
			get_all=Mock(
				side_effect=[
					[{"total": 1}],
					[{"name": "INQ-2026-00001", "status": "Inactive"}],
				]
			)
		)
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch(
			"qas_custom.services.school_admin._build_inquiry_list_item", side_effect=lambda row: row,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = get_school_admin_inquiries_data(
				status="Inactive",
				inquiry_type="Trial Lesson",
				query="roger@example.com",
				limit_start=0,
				limit=100,
			)

		count_call, page_call = fake_frappe.get_all.call_args_list
		self.assertEqual(count_call.kwargs["filters"]["status"], "Inactive")
		self.assertEqual(count_call.kwargs["filters"]["inquiry_type"], "Trial Lesson")
		self.assertEqual(page_call.kwargs["filters"], count_call.kwargs["filters"])
		search_fields = {entry[1] for entry in page_call.kwargs["or_filters"]}
		self.assertEqual(
			search_fields,
			{"name", "parent", "student", "contact_name", "contact_phone", "contact_email", "submitted_student_name"},
		)
		self.assertEqual(result["total"], 1)
		self.assertFalse(result["has_more"])

	def test_pagination_returns_stable_metadata_and_offset(self):
		fake_frappe = SimpleNamespace(
			get_all=Mock(
				side_effect=[
					[{"total": 3}],
					[{"name": "INQ-2026-00002"}],
				]
			)
		)
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch(
			"qas_custom.services.school_admin._build_inquiry_list_item", side_effect=lambda row: row,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = get_school_admin_inquiries_data(limit_start=1, limit=1)

		page_call = fake_frappe.get_all.call_args_list[1]
		self.assertEqual(page_call.kwargs["limit_start"], 1)
		self.assertEqual(page_call.kwargs["limit_page_length"], 1)
		self.assertIn("name desc", page_call.kwargs["order_by"])
		self.assertEqual(result["limit_start"], 1)
		self.assertEqual(result["limit"], 1)
		self.assertEqual(result["total"], 3)
		self.assertTrue(result["has_more"])

	def test_post_visit_and_booked_are_combined_with_today_as_date_boundary(self):
		fake_frappe = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 1}], [{"name": "INQ-2026-00003"}]]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch(
			"qas_custom.services.school_admin._build_inquiry_list_item", side_effect=lambda row: row,
		), patch(
			"qas_custom.services.school_admin.nowdate", return_value="2026-07-22",
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			get_school_admin_inquiries_data(status="Booked", queue="post_visit")

		count_call, page_call = fake_frappe.get_all.call_args_list
		self.assertEqual(count_call.kwargs["filters"]["status"], "Booked")
		self.assertEqual(
			count_call.kwargs["filters"]["current_appointment_date"],
			["<=", getdate("2026-07-22")],
		)
		self.assertEqual(page_call.kwargs["filters"], count_call.kwargs["filters"])
		self.assertTrue(page_call.kwargs["order_by"].startswith("current_appointment_date desc"))

	def test_negative_offset_is_normalized_to_first_page(self):
		fake_frappe = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 0}], []]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = get_school_admin_inquiries_data(limit_start=-20)

		self.assertEqual(fake_frappe.get_all.call_args_list[1].kwargs["limit_start"], 0)
		self.assertEqual(result["limit_start"], 0)
