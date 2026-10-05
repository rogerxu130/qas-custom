"""PAYG timetable classification and commit-scoped staff notifications."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.modules.notifications import commands
from qas_custom.qas_custom.doctype.qas_payg_booking.qas_payg_booking import QASPAYGBooking
from qas_custom.services import school_admin


class TestPaygStaffNotifications(TestCase):
    def test_finalized_booking_queues_once_and_cancellation_notifies(self):
        doc = SimpleNamespace(name="B", student="S", course_session="CS", status="Reserved",
            attendance_entry=None, get_doc_before_save=lambda: None)
        with patch.object(commands, "enqueue_session_staff_notification") as enqueue:
            QASPAYGBooking.on_update(doc)
            enqueue.assert_not_called()
            doc.attendance_entry = "ATT"
            doc.get_doc_before_save = lambda: {"status": "Reserved", "attendance_entry": None}
            QASPAYGBooking.on_update(doc)
            enqueue.assert_called_once_with("payg_booked", course_session="CS", student="S",
                source_doctype="QAS PAYG Booking", source_document="B")
            enqueue.reset_mock()
            doc.get_doc_before_save = lambda: {"status": "Reserved", "attendance_entry": "ATT"}
            QASPAYGBooking.on_update(doc)
            enqueue.assert_not_called()
            doc.status = "Cancelled"
            QASPAYGBooking.on_update(doc)
            self.assertEqual(enqueue.call_args.args, ("payg_cancelled",))
            enqueue.reset_mock()
            doc.get_doc_before_save = lambda: {"status": "Cancelled", "attendance_entry": "ATT"}
            QASPAYGBooking.on_update(doc)
            enqueue.assert_not_called()

    def test_notification_queue_failure_does_not_fail_booking_update(self):
        from qas_custom.qas_custom.doctype.qas_payg_booking import qas_payg_booking as controller
        doc = SimpleNamespace(name="B", student="S", course_session="CS", status="Reserved",
            attendance_entry="ATT", get_doc_before_save=lambda: {"attendance_entry": None})
        fake = SimpleNamespace(log_error=Mock(), get_traceback=lambda: "queue unavailable")
        with patch.object(commands, "enqueue_session_staff_notification", side_effect=RuntimeError("queue unavailable")), \
                patch.object(controller, "frappe", fake):
            QASPAYGBooking.on_update(doc)
        fake.log_error.assert_called_once()

    def test_payg_recipients_include_teacher_and_enabled_school_admins_deduplicated(self):
        with patch.object(commands, "_session_staff_course_context", return_value={
            "teacher_recipients": ["teacher@example.test"], "school_email": "school@example.test"}), \
                patch.object(commands, "_payg_school_admin_emails", return_value=["admin@example.test", "teacher@example.test"]):
            result = commands._session_staff_notification_context("payg_booked", "CS", "S")
        self.assertEqual(result["recipients"], ["teacher@example.test", "school@example.test", "admin@example.test"])

    def test_school_admin_recipient_query_excludes_disabled_users(self):
        fake = SimpleNamespace(get_all=Mock(side_effect=[["admin", "disabled"], [" ADMIN@example.test "]]))
        with patch.object(commands, "frappe", fake):
            self.assertEqual(commands._payg_school_admin_emails(), ["admin@example.test"])
        self.assertEqual(fake.get_all.call_args.kwargs["filters"]["enabled"], 1)

    def test_staff_job_rejects_cancelled_or_other_student_bookings(self):
        fake = SimpleNamespace(db=SimpleNamespace(get_value=Mock()))
        with patch.object(commands, "frappe", fake):
            for status in ("Reserved", "Locked", "Completed", "Cancelled"):
                fake.db.get_value.return_value = frappe._dict(status=status, student="S", course_session="CS", attendance_entry="ATT")
                self.assertEqual(commands._session_staff_notification_is_current("payg_booked", "CS", "S", "B"), status != "Cancelled")
                self.assertEqual(commands._session_staff_notification_is_current("payg_cancelled", "CS", "S", "B"), status == "Cancelled")
            self.assertFalse(commands._session_staff_notification_is_current("payg_cancelled", "OTHER", "S", "B"))
            self.assertFalse(commands._session_staff_notification_is_current("payg_cancelled", "CS", "OTHER", "B"))

    def test_queue_after_commit_and_duplicate_retry_skipped(self):
        fake = SimpleNamespace(enqueue=Mock())
        context = {"recipients": ["teacher@example.test", "admin@example.test"], "missing_recipients": []}
        with patch.object(commands, "frappe", fake), \
                patch.object(commands, "_session_staff_notification_context", return_value=context), \
                patch.object(commands, "_session_staff_notification_subject", return_value="PAYG booking"), \
                patch.object(commands, "_session_staff_notification_email_message", return_value="Details"), \
                patch.object(commands, "_session_staff_notification_already_logged", side_effect=[False, True]), \
                patch.object(commands, "_create_notification_log", return_value="LOG"), \
                patch.object(commands, "_mark_notification_queued"), \
                patch.object(commands, "outbound_email_enabled", return_value=True):
            for _ in range(2):
                commands.enqueue_session_staff_notification("payg_booked", course_session="CS", student="S", source_doctype="QAS PAYG Booking", source_document="B")
        fake.enqueue.assert_called_once()
        self.assertTrue(fake.enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(fake.enqueue.call_args.kwargs["notification_event"], "payg_booked")

    def test_payg_email_names_student_and_class_details(self):
        context = {"event": "payg_booked", "school_name": "Queensland Art School", "student_name": "Ava & Ben",
            "course": "Anime Art", "campus": "Indooroopilly", "classroom": "Room 1", "day_of_week": "Saturday",
            "date_display": "10 October 2026", "start_time": "14:40", "end_time": "16:10"}
        with patch.object(commands, "_", side_effect=lambda value: value):
            message = commands._session_staff_notification_email_message(context)
            subject = commands._session_staff_notification_subject(context)
        self.assertIn("Ava &amp; Ben", message)
        self.assertIn("PSU Go class pass", message)
        self.assertIn("Anime Art", message)
        self.assertIn("Saturday 10 October 2026", message)
        self.assertIn("14:40 - 16:10", message)
        self.assertIn("Pay-as-you-go class booked", subject)

    def test_counts_include_manual_and_pass_bookings_once(self):
        rows = [frappe._dict(course_session="CS", source_doctype="QAS PAYG Booking", enrollment_type="Pay-as-you-go"),
                frappe._dict(course_session="CS", source_doctype=None, enrollment_type="Pay-as-you-go"),
                frappe._dict(course_session="CS", source_doctype="Inquiry", enrollment_type="Trial")]
        with patch.object(school_admin, "_doctype_available", return_value=True), \
                patch.object(school_admin, "_has_field", return_value=True), \
                patch.object(school_admin, "_safe_fields", side_effect=lambda dt, fields: fields), \
                patch.object(school_admin.frappe, "get_all", return_value=rows) as get_all:
            self.assertEqual(school_admin._get_course_session_payg_counts(["CS"]), {"CS": 2})
        self.assertEqual(get_all.call_args.kwargs["filters"]["status"], ["not in", ["Cancelled", "Leave"]])
        self.assertEqual(school_admin._count_payg_attendance_rows(rows), 2)
