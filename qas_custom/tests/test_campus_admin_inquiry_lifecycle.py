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
	def test_time_queues_do_not_apply_lifecycle_status_filters(self):
		for status in [None, "Needs Review", "Cancelled", "Completed", "No-show", "Parked", "Converted", "Inactive"]:
			for queue, operator in [("upcoming", ">="), ("post_trial", "<")]:
				with self.subTest(status=status, queue=queue):
					filters, or_filters = _campus_admin_inquiry_queue_filters(queue, status=status, reference_date="2026-07-17")
					self.assertEqual(filters, {"current_appointment_date": [operator, getdate("2026-07-17")]})
					self.assertIsNone(or_filters)

	def test_all_time_needs_review_has_no_appointment_constraint(self):
		filters, or_filters = _campus_admin_inquiry_queue_filters("all", status="Needs Review")
		self.assertEqual(filters, {})
		self.assertIsNone(or_filters)

	def test_campus_service_intersects_past_date_and_exact_status(self):
		with patch("qas_custom.services.campus_admin._require_campus_admin_profile", return_value={"campuses": ["Indooroopilly"]}), patch(
			"qas_custom.services.campus_admin._filter_requested_campus", return_value=["Indooroopilly"]
		), patch("qas_custom.services.inquiry_filters.get_datetime_in_timezone", return_value="2026-07-17"), patch(
			"qas_custom.services.campus_admin.frappe.get_all", return_value=[]
		) as get_all:
			get_campus_admin_inquiries_data(queue="post_trial", status="Cancelled")
		kwargs = get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"], {"campus": ["in", ["Indooroopilly"]], "status": "Cancelled", "current_appointment_date": ["<=", getdate("2026-07-16")]})
		self.assertIsNone(kwargs["or_filters"])

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
