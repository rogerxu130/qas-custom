from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from frappe.utils import getdate

from qas_custom.services.school_admin import (
	_attach_inquiry_teacher_labels,
	get_school_admin_inquiries_data,
)


class TestSchoolAdminInquiryList(TestCase):
	def test_multiple_statuses_preserve_both_legacy_aliases(self):
		from qas_custom.services.inquiry_filters import inquiry_status_filter
		self.assertEqual(inquiry_status_filter("Needs Review,Booked"), ["in", ["Needs Review", "Booked", "Rescheduled", "Planned"]])
		self.assertEqual(inquiry_status_filter("Needs Review"), ["in", ["Needs Review", "Planned"]])

	def test_pending_enrollment_queue_intersects_selected_statuses(self):
		fake = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 0}], []]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin._safe_fields", side_effect=lambda d, f: f), patch("qas_custom.services.school_admin.frappe", fake):
			get_school_admin_inquiries_data(queue="enrollment_pending", status="Needs Review,Booked")
		self.assertEqual(fake.get_all.call_args.kwargs["filters"]["status"], ["in", ["Needs Review", "Planned"]])
		self.assertEqual(fake.get_all.call_args.kwargs["filters"]["inquiry_type"], "Direct Enrollment")

	def test_multiple_statuses_share_count_and_page_filters(self):
		fake = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 0}], []]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin._safe_fields", side_effect=lambda d, f: f), patch("qas_custom.services.school_admin.frappe", fake):
			get_school_admin_inquiries_data(status="Booked, No-show,Follow-up,Booked", query="Amy", from_date="2026-10-12", to_date="2026-10-12", limit_start=80)
		count, page = fake.get_all.call_args_list
		self.assertEqual(page.kwargs["filters"], count.kwargs["filters"])
		self.assertEqual(page.kwargs["filters"]["status"], ["in", ["Booked", "No-show", "Follow-up", "Rescheduled"]])
		self.assertEqual(page.kwargs["filters"]["current_appointment_date"], ["between", [getdate("2026-10-12"), getdate("2026-10-12")]])
		self.assertEqual(page.kwargs["limit_start"], 80)
		self.assertTrue(page.kwargs["or_filters"])

	def test_multiple_statuses_reject_unsupported_value(self):
		fake = SimpleNamespace(throw=Mock(side_effect=ValueError("Unsupported")))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin.frappe", fake), patch("qas_custom.services.school_admin._", side_effect=lambda value: value):
			with self.assertRaises(ValueError):
				get_school_admin_inquiries_data(status="Booked,Unknown")

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
		self.assertIn("review_reason", page_call.kwargs["fields"])
		search_fields = {entry[1] for entry in page_call.kwargs["or_filters"]}
		self.assertEqual(
			search_fields,
			{"name", "parent", "student", "contact_name", "contact_phone", "contact_email", "submitted_student_name"},
		)
		self.assertEqual(result["total"], 1)
		self.assertFalse(result["has_more"])
		self.assertEqual(result["items"][0]["teacher_display"], "")

	def test_teacher_labels_use_session_override_then_weekly_teacher_in_bulk(self):
		items = [
			{"id": "INQ-001", "course_session": "CS-001"},
			{"id": "INQ-002", "course_session": "CS-002"},
			{"id": "INQ-003", "course_session": "CS-MISSING"},
			{"id": "INQ-004", "course_session": ""},
		]
		fake_frappe = SimpleNamespace(
			get_all=Mock(
				side_effect=[
					[
						{"name": "CS-001", "weekly_timeslot": "WT-001", "teacher_override": "TEACHER-OVERRIDE"},
						{"name": "CS-002", "weekly_timeslot": "WT-002", "teacher_override": ""},
					],
					[{"name": "WT-002", "teacher": "TEACHER-WEEKLY"}],
					[
						{"name": "TEACHER-OVERRIDE", "teacher_name": "Joanne"},
						{"name": "TEACHER-WEEKLY", "teacher_name": "Marco"},
					],
				]
			)
		)

		with patch("qas_custom.services.school_admin.frappe", fake_frappe):
			_attach_inquiry_teacher_labels(items)

		self.assertEqual(items[0]["teacher"], "TEACHER-OVERRIDE")
		self.assertEqual(items[0]["teacher_display"], "Joanne")
		self.assertEqual(items[0]["teacher_assignment_source"], "Session override")
		self.assertEqual(items[1]["teacher"], "TEACHER-WEEKLY")
		self.assertEqual(items[1]["teacher_display"], "Marco")
		self.assertEqual(items[1]["teacher_assignment_source"], "Weekly timeslot")
		self.assertEqual(items[2]["teacher_display"], "")
		self.assertEqual(items[3]["teacher_display"], "")
		self.assertEqual(fake_frappe.get_all.call_count, 3)
		self.assertEqual(
			fake_frappe.get_all.call_args_list[0].kwargs["filters"]["name"],
			["in", ["CS-001", "CS-002", "CS-MISSING"]],
		)

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

	def test_past_and_booked_are_combined_with_yesterday_as_date_boundary(self):
		fake_frappe = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 1}], [{"name": "INQ-2026-00003"}]]))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields,
		), patch(
			"qas_custom.services.school_admin._build_inquiry_list_item", side_effect=lambda row: row,
		), patch(
			"qas_custom.services.inquiry_filters.get_datetime_in_timezone", return_value="2026-07-22",
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			get_school_admin_inquiries_data(status="Booked", queue="post_visit")

		count_call, page_call = fake_frappe.get_all.call_args_list
		self.assertEqual(count_call.kwargs["filters"]["status"], ["in", ["Booked", "Rescheduled"]])
		self.assertEqual(
			count_call.kwargs["filters"]["current_appointment_date"],
			["<=", getdate("2026-07-21")],
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


class TestPendingEnrollmentQueue(TestCase):
	def test_pending_queue_includes_both_states_without_date_cutoff(self):
		for status in (None, "Planned", "Needs Review"):
			with self.subTest(status=status):
				fake = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 0}], []]))
				with patch("qas_custom.services.school_admin._require_school_admin"), patch(
					"qas_custom.services.school_admin._safe_fields", side_effect=lambda _doctype, fields: fields
				), patch("qas_custom.services.school_admin.frappe", fake):
					get_school_admin_inquiries_data(queue="enrollment_pending", status=status)
				filters = fake.get_all.call_args_list[0].kwargs["filters"]
				self.assertEqual(filters["inquiry_type"], "Direct Enrollment")
				self.assertEqual(filters["status"], ["in", ["Needs Review", "Planned"]] if status == "Needs Review" else status or ["in", ["Planned", "Needs Review"]])
				self.assertNotIn("current_appointment_date", filters)
