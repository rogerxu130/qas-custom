from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from frappe.utils import getdate

from qas_custom.services.campus_admin import (
	POST_VISIT_INQUIRY_STATUSES,
	_campus_admin_inquiry_queue_filters,
	get_campus_admin_inquiries_data,
	reopen_campus_admin_inquiry_data,
)


class TestCampusAdminInquiryLifecycleQueues(TestCase):
	def test_upcoming_excludes_outcome_statuses_even_when_future_dated(self):
		filters, or_filters = _campus_admin_inquiry_queue_filters("upcoming", reference_date="2026-07-17")
		self.assertEqual(filters["current_appointment_date"], [">=", getdate("2026-07-17")])
		self.assertEqual(filters["status"], ["not in", list(POST_VISIT_INQUIRY_STATUSES)])
		self.assertIsNone(or_filters)

		filters, _ = _campus_admin_inquiry_queue_filters(
			"upcoming",
			status="Completed",
			reference_date="2026-07-17",
		)
		self.assertEqual(filters, {"name": "__qas_no_matching_inquiry__"})

	def test_post_visit_includes_outcome_statuses_regardless_of_date(self):
		filters, or_filters = _campus_admin_inquiry_queue_filters("post_trial", reference_date="2026-07-17")
		self.assertEqual(filters, {})
		self.assertIn(
			["Inquiry", "status", "in", list(POST_VISIT_INQUIRY_STATUSES)],
			or_filters,
		)
		self.assertIn(["Inquiry", "current_appointment_date", "<", getdate("2026-07-17")], or_filters)

		filters, or_filters = _campus_admin_inquiry_queue_filters(
			"post_trial",
			status="Completed",
			reference_date="2026-07-17",
		)
		self.assertEqual(filters, {})
		self.assertIsNone(or_filters)

	def test_past_booked_inquiry_remains_in_post_visit(self):
		filters, or_filters = _campus_admin_inquiry_queue_filters(
			"post_trial",
			status="Booked",
			reference_date="2026-07-17",
		)
		self.assertEqual(filters, {"current_appointment_date": ["<", getdate("2026-07-17")]})
		self.assertIsNone(or_filters)

	def test_inquiry_service_applies_post_visit_or_filters(self):
		with patch(
			"qas_custom.services.campus_admin._require_campus_admin_profile",
			return_value={"campuses": ["Indooroopilly"]},
		), patch(
			"qas_custom.services.campus_admin._filter_requested_campus",
			return_value=["Indooroopilly"],
		), patch("qas_custom.services.campus_admin.today", return_value="2026-07-17"), patch(
			"qas_custom.services.campus_admin.frappe.get_all",
			return_value=[],
		) as get_all:
			self.assertEqual(get_campus_admin_inquiries_data(queue="post_trial"), {"items": []})

		kwargs = get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"], {"campus": ["in", ["Indooroopilly"]]})
		self.assertIn(
			["Inquiry", "status", "in", list(POST_VISIT_INQUIRY_STATUSES)],
			kwargs["or_filters"],
		)

	def test_reopen_completed_restores_booked_and_preserves_booking(self):
		inquiry = SimpleNamespace(
			name="INQ-001",
			status="Completed",
			course_session="CS-001",
			current_appointment_date="2026-07-21",
			current_appointment_time="09:00:00",
			review_reason="Existing review",
			save=Mock(),
		)
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="campus@example.com"),
			db=SimpleNamespace(commit=Mock()),
			get_doc=Mock(return_value=inquiry),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.campus_admin.reject_support_view_write"), patch(
			"qas_custom.services.campus_admin._require_inquiry_access"
		), patch("qas_custom.services.campus_admin._add_system_inquiry_note") as add_note, patch(
			"qas_custom.services.campus_admin.build_inquiry_detail",
			return_value={"inquiry": {"id": "INQ-001", "status": "Booked"}},
		), patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			result = reopen_campus_admin_inquiry_data("INQ-001")

		self.assertEqual(inquiry.status, "Booked")
		self.assertEqual(inquiry.course_session, "CS-001")
		self.assertEqual(inquiry.current_appointment_date, "2026-07-21")
		self.assertEqual(inquiry.current_appointment_time, "09:00:00")
		self.assertIsNone(inquiry.review_reason)
		inquiry.save.assert_called_once_with(ignore_permissions=True)
		add_note.assert_called_once()
		fake_frappe.db.commit.assert_called_once()
		self.assertEqual(result["inquiry"]["status"], "Booked")

	def test_cancelled_reopen_keeps_existing_restore_behavior(self):
		inquiry = SimpleNamespace(
			name="INQ-002",
			status="Cancelled",
			course_session="CS-002",
			current_appointment_date="2026-07-22",
			current_appointment_time="10:00:00",
			review_reason="Cancelled",
			save=Mock(),
		)
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="campus@example.com"),
			db=SimpleNamespace(commit=Mock()),
			get_doc=Mock(return_value=inquiry),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.campus_admin.reject_support_view_write"), patch(
			"qas_custom.services.campus_admin._require_inquiry_access"
		), patch("qas_custom.services.campus_admin._add_system_inquiry_note"), patch(
			"qas_custom.services.campus_admin.build_inquiry_detail",
			return_value={"inquiry": {"id": "INQ-002", "status": "Booked"}},
		), patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			reopen_campus_admin_inquiry_data("INQ-002")

		self.assertEqual(inquiry.status, "Booked")
		self.assertEqual(inquiry.course_session, "CS-002")
		inquiry.save.assert_called_once_with(ignore_permissions=True)

	def test_reopen_rejects_follow_up(self):
		inquiry = SimpleNamespace(name="INQ-001", status="Follow-up")
		fake_frappe = SimpleNamespace(
			get_doc=Mock(return_value=inquiry),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.campus_admin.reject_support_view_write"), patch(
			"qas_custom.services.campus_admin._require_inquiry_access"
		), patch("qas_custom.services.campus_admin.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "completed or cancelled"):
				reopen_campus_admin_inquiry_data("INQ-001")
