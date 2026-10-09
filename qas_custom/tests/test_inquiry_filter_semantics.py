from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from frappe.utils import getdate
from qas_custom.services.inquiry_filters import inquiry_queue_filters, inquiry_date_filter
from qas_custom.services.inquiry import reschedule_inquiry_core
from qas_custom.services.school_admin import get_school_admin_inquiries_data
from qas_custom.modules.notifications.trial_parent_notifications import classify_trial_booking_change
from qas_custom.modules.notifications.school_visit_parent_notifications import classify_school_visit_booking_change


class TestInquiryFilterSemantics(TestCase):
    def test_explicit_dates_intersect_queue_boundary(self):
        upcoming = inquiry_queue_filters("upcoming", reference_date="2026-10-09")["current_appointment_date"]
        past = inquiry_queue_filters("post_visit", reference_date="2026-10-09")["current_appointment_date"]
        self.assertEqual(inquiry_date_filter(upcoming, from_date="2026-10-01", to_date="2026-10-12"), ["between", [getdate("2026-10-09"), getdate("2026-10-12")]])
        self.assertEqual(inquiry_date_filter(past, from_date="2026-10-01", to_date="2026-10-12"), ["between", [getdate("2026-10-01"), getdate("2026-10-08")]])
        self.assertIs(inquiry_date_filter(past, from_date="2026-10-09"), False)
        self.assertIs(inquiry_date_filter(upcoming, to_date="2026-10-08"), False)

    def test_all_time_needs_review_does_not_require_date(self):
        self.assertEqual(inquiry_queue_filters("", status="Needs Review"), {})
        self.assertEqual(inquiry_queue_filters("all", status="Needs Review"), {})
        self.assertIsNone(inquiry_date_filter())

    def test_past_aliases_share_the_same_boundary(self):
        self.assertEqual(inquiry_queue_filters("post_visit", reference_date="2026-10-09"), inquiry_queue_filters("post_trial", reference_date="2026-10-09"))

    def test_legacy_lifecycle_queues_remain_compatible(self):
        self.assertEqual(inquiry_queue_filters("needs_scheduling"), {"status": "Needs Review"})
        self.assertEqual(inquiry_queue_filters("parked"), {"status": "Parked"})
        self.assertIn("name", inquiry_queue_filters("parked", status="Booked"))

    def test_school_status_and_search_are_preserved_for_each_time_range(self):
        for queue in ["upcoming", "post_visit", ""]:
            for status in ["", "Needs Review", "Cancelled", "Completed", "No-show", "Parked", "Converted", "Inactive"]:
                with self.subTest(queue=queue, status=status):
                    fake = SimpleNamespace(get_all=Mock(side_effect=[[{"total": 0}], []]))
                    with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin._safe_fields", side_effect=lambda d, f: f), patch("qas_custom.services.inquiry_filters.get_datetime_in_timezone", return_value="2026-10-09"), patch("qas_custom.services.school_admin.frappe", fake):
                        get_school_admin_inquiries_data(queue=queue, status=status, query="Amy", inquiry_type="Trial Lesson")
                    count, page = fake.get_all.call_args_list
                    filters = page.kwargs["filters"]
                    self.assertEqual(filters, count.kwargs["filters"])
                    self.assertEqual(filters.get("status"), ["in", ["Needs Review", "Planned"]] if status == "Needs Review" else status or None)
                    self.assertEqual(filters["inquiry_type"], "Trial Lesson")
                    self.assertTrue(page.kwargs["or_filters"])
                    if queue == "upcoming":
                        self.assertEqual(filters["current_appointment_date"], [">=", getdate("2026-10-09")])
                    elif queue == "post_visit":
                        self.assertEqual(filters["current_appointment_date"], ["<=", getdate("2026-10-08")])
                    else:
                        self.assertNotIn("current_appointment_date", filters)

    def test_inverted_explicit_dates_are_rejected(self):
        with patch("qas_custom.services.inquiry_filters.frappe.throw", side_effect=ValueError):
            with self.assertRaises(ValueError):
                inquiry_date_filter(from_date="2026-10-10", to_date="2026-10-09")


class TestRescheduleStaysBooked(TestCase):
    def test_trial_reschedule_assigns_booked(self):
        doc = SimpleNamespace(inquiry_type="Trial Lesson", status="Booked", name="INQ-1")
        with patch("qas_custom.services.inquiry.frappe.get_doc", return_value=doc), patch("qas_custom.services.inquiry._normalize_inquiry_payload", side_effect=lambda p:p), patch("qas_custom.services.inquiry.assign_inquiry_course_session_core") as assign:
            reschedule_inquiry_core("INQ-1", {"course_session": "CS-new"})
        assign.assert_called_once_with("INQ-1", "CS-new", status="Booked")

    def test_visit_reschedule_saves_booked(self):
        doc = SimpleNamespace(inquiry_type="School Visit", status="Booked", name="INQ-1", save=Mock())
        with patch("qas_custom.services.inquiry.frappe.get_doc", return_value=doc), patch("qas_custom.services.inquiry._normalize_inquiry_payload", side_effect=lambda p:p), patch("qas_custom.services.inquiry._parse_appointment_datetime", return_value=("2026-10-15", "10:00:00")), patch("qas_custom.services.inquiry.frappe.db", SimpleNamespace(commit=Mock())), patch("qas_custom.services.inquiry.build_inquiry_detail"):
            reschedule_inquiry_core("INQ-1", {})
        self.assertEqual(doc.status, "Booked")
        self.assertEqual(doc.current_appointment_date, "2026-10-15")
        doc.save.assert_called_once_with(ignore_permissions=True)

    def test_trial_change_still_classifies_as_reschedule_with_booked_status(self):
        old = {"status": "Booked", "course_session": "CS-old"}
        new = {"inquiry_type": "Trial Lesson", "status": "Booked", "course_session": "CS-new"}
        self.assertEqual(classify_trial_booking_change(new, old), "rescheduled")
        self.assertIsNone(classify_trial_booking_change(new, new))

    def test_visit_change_still_classifies_as_reschedule_with_booked_status(self):
        old = {"inquiry_type": "School Visit", "status": "Booked", "campus": "Indooroopilly", "contact_email": "parent@example.com", "current_appointment_date": "2026-10-10", "current_appointment_time": "10:00:00"}
        new = {**old, "current_appointment_time": "11:00:00"}
        self.assertEqual(classify_school_visit_booking_change(new, old), "rescheduled")
        self.assertIsNone(classify_school_visit_booking_change(new, new))
