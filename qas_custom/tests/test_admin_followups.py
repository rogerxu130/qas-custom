from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import admin_followups as service


class TestFollowupQueue(TestCase):
    def row(self, **changes):
        return {"attendance": "A1", "student": "S1", "student_name": "Amy", "enrollment_type": "Makeup", "status": "To be started", "session_id": "CS", "session_date": "2026-09-10", "start_time": "10:00:00", "end_time": "11:00:00", "campus": "City", "campus_address": "1 Example Street", "contact_phone": "0400000000", "makeup_voucher": "MV", **changes}

    def test_only_one_makeup_student_becomes_a_followup(self):
        self.assertEqual(len(service.build_followups([self.row()], {})), 1)
        for other in (self.row(student="S2", makeup_voucher="MV2"), self.row(student="S2", enrollment_type="Trial", makeup_voucher=None), self.row(student="S2", enrollment_type="Full-Term", makeup_voucher=None)):
            self.assertEqual(service.build_followups([self.row(), other], {}), [])

    def test_leave_and_cancelled_students_do_not_mask_a_solo_booking(self):
        for status in ("Leave", "Cancelled"):
            rows = [self.row(), self.row(student="S2", status=status, enrollment_type="Full-Term", makeup_voucher=None)]
            self.assertEqual(len(service.build_followups(rows, {})), 1)
        self.assertEqual(service.build_followups([self.row(status="Cancelled")], {}), [])

    def test_duplicate_roster_rows_do_not_duplicate_the_student(self):
        self.assertEqual(len(service.build_followups([self.row(), self.row(attendance="A2", makeup_voucher="MV2")], {})), 1)

    def test_sent_and_confirmed_complete_the_current_booking(self):
        item = service.build_followups([self.row()], {})[0]
        for status in service.COMPLETE:
            items = service.build_followups([self.row()], {item["key"]: {"contact_status": status}})
            self.assertFalse(items[0]["pending"])
        self.assertTrue(service.build_followups([self.row()], {item["key"]: {"contact_status": "Pending"}})[0]["pending"])

    def test_changed_session_time_or_voucher_requires_new_contact(self):
        item = service.build_followups([self.row()], {})[0]
        contacts = {item["key"]: {"contact_status": "Text Message Sent"}}
        for change in ({"session_id": "NEW"}, {"session_date": "2026-09-11"}, {"start_time": "11:00:00"}, {"makeup_voucher": "NEW"}, {"campus_address": "2 Other Street"}):
            new = service.build_followups([self.row(**change)], contacts)[0]
            self.assertTrue(new["pending"])
            self.assertNotEqual(new["key"], item["key"])

    def test_trial_uses_existing_contact_status_and_ignores_old_session(self):
        trial = self.row(enrollment_type="Trial", makeup_voucher=None, inquiry="I", inquiry_type="Trial Lesson", inquiry_status="Booked", inquiry_session="CS", inquiry_phone="0411111111", confirmation_status="Text Message Sent")
        item = service.build_followups([trial], {})[0]
        self.assertEqual(item["kind"], "trial")
        self.assertFalse(item["pending"])
        self.assertEqual(item["contact_phone"], "0411111111")
        self.assertEqual(service.build_followups([{**trial, "inquiry_session": "OTHER"}], {}), [])
        self.assertEqual(service.build_followups([{**trial, "inquiry_status": "Cancelled"}], {}), [])

    def test_missing_phone_remains_visible_but_cannot_prepare_sms(self):
        item = service.build_followups([self.row(contact_phone="")], {})[0]
        self.assertTrue(item["pending"])
        self.assertFalse(item["sms_ready"])

    def test_stale_contact_request_does_not_write(self):
        fake = SimpleNamespace(throw=Mock(side_effect=ValueError("stale")), new_doc=Mock())
        with patch.object(service, "require_admin"), patch.object(service, "collect_followups", return_value=[]), patch.object(service, "frappe", fake):
            with self.assertRaises(ValueError):
                service.update_contact("old", "2026-09-10", "Text Message Sent")
        fake.new_doc.assert_not_called()


class TestFollowupDigest(TestCase):
    def fake(self):
        settings = SimpleNamespace(enabled=1, recipient="admin@example.com", portal_url="https://portal.example.com")
        doc = Mock()
        return SimpleNamespace(get_single=Mock(return_value=settings), get_doc=Mock(return_value=doc), cache=SimpleNamespace(lock=Mock(side_effect=lambda *a, **kw: nullcontext())), db=SimpleNamespace(exists=Mock(return_value=False), commit=Mock()))

    def test_reminder_is_queued_once_per_window_and_only_for_pending_items(self):
        fake = self.fake()
        items = [{"kind": "trial", "pending": False}, {"kind": "solo_makeup", "pending": True}]
        with patch.object(service, "frappe", fake), patch.object(service, "collect_followups", return_value=items), patch.object(service, "sendmail_or_skip", return_value=None) as mail:
            result = service.run_digest(datetime(2026, 9, 9, 10))
            self.assertEqual(result, {"queued": True, "count": 1})
            fake.db.exists.return_value = True
            self.assertTrue(service.run_digest(datetime(2026, 9, 9, 10))["skipped"])
        mail.assert_called_once()
        self.assertTrue(mail.call_args.kwargs["delayed"])
        self.assertIn("0 位试课学生、0 个参观预约和 1 位单人补课学生", mail.call_args.kwargs["message"])

    def test_evening_rechecks_and_includes_new_bookings(self):
        fake = self.fake()
        with patch.object(service, "frappe", fake), patch.object(service, "collect_followups", side_effect=[[{"kind": "trial", "pending": True}], [{"kind": "trial", "pending": False}, {"kind": "solo_makeup", "pending": True}]]), patch.object(service, "sendmail_or_skip", return_value=None) as mail:
            service.run_digest(datetime(2026, 9, 9, 10))
            service.run_digest(datetime(2026, 9, 9, 18))
        self.assertEqual(mail.call_count, 2)
        keys = [call.kwargs["set_name"] for call in fake.get_doc.return_value.insert.call_args_list]
        self.assertNotEqual(keys[0], keys[1])

    def test_no_email_when_done_disabled_or_outside_window(self):
        fake = self.fake()
        with patch.object(service, "frappe", fake), patch.object(service, "collect_followups", return_value=[]), patch.object(service, "sendmail_or_skip") as mail:
            for hour in (9, 10, 18, 19):
                self.assertTrue(service.run_digest(datetime(2026, 9, 9, hour))["skipped"])
            fake.get_single.return_value.enabled = 0
            self.assertTrue(service.run_digest(datetime(2026, 9, 9, 10))["skipped"])
        mail.assert_not_called()
        fake.get_doc.assert_not_called()

    def test_blocked_email_does_not_record_completion(self):
        fake = self.fake()
        with patch.object(service, "frappe", fake), patch.object(service, "collect_followups", return_value=[{"kind": "trial", "pending": True}]), patch.object(service, "sendmail_or_skip", return_value={"skipped": True}):
            self.assertTrue(service.run_digest(datetime(2026, 9, 9, 10))["skipped"])
        fake.get_doc.assert_not_called()

    def test_email_links_to_selected_date_and_followup_page(self):
        body = service.digest_message("2026-09-10", 18, [{"kind": "solo_makeup"}], "https://portal.example.com")
        self.assertIn("tab=followups", body)
        self.assertIn("date=2026-09-10", body)
        self.assertIn("明日仍有未完成", body)


class TestVisitFollowups(TestCase):
    def visit(self, **changes):
        return dict(reference="VISIT1", inquiry_status="Booked", student=None,
                    contact_name="Sam", contact_phone="0400000000", campus="City",
                    campus_address="1 Example Street", session_date="2026-09-10",
                    start_time="10:00:00", confirmation_status="Pending", **changes)

    def test_visit_without_student_or_attendance_is_collected(self):
        fake = SimpleNamespace(db=SimpleNamespace(sql=Mock(side_effect=[[], [self.visit()]])), get_all=Mock(return_value=[]))
        with patch.object(service, "frappe", fake):
            items = service.collect_followups("2026-09-10", "2026-09-10")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "visit")
        self.assertTrue(items[0]["pending"])
        self.assertTrue(items[0]["sms_ready"])

    def test_visit_status_and_changed_appointment(self):
        row = self.visit()
        original = service.build_visit_followups([row])[0]
        for status in service.COMPLETE:
            self.assertFalse(service.build_visit_followups([{**row, "confirmation_status": status}])[0]["pending"])
        self.assertEqual(service.build_visit_followups([{**row, "inquiry_status": "Cancelled"}]), [])
        changed = service.build_visit_followups([{**row, "session_date": "2026-09-11", "inquiry_status": "Rescheduled"}])[0]
        self.assertNotEqual(original["key"], changed["key"])
        self.assertTrue(changed["pending"])

    def test_visit_record_validates_inquiry_appointment(self):
        item = service.build_visit_followups([self.visit()])[0]
        doc = Mock()
        fake = SimpleNamespace(cache=SimpleNamespace(lock=Mock(return_value=nullcontext())),
                               db=SimpleNamespace(exists=Mock(return_value=False), commit=Mock()),
                               new_doc=Mock(return_value=doc), session=SimpleNamespace(user="admin"))
        with patch.object(service, "frappe", fake), patch.object(service, "require_admin"), patch.object(service, "collect_followups", return_value=[item]), patch.object(service, "now_datetime", return_value=datetime(2026, 9, 9, 10)), patch('qas_custom.services.inquiry.update_inquiry_confirmation_core') as confirm:
            result = service.update_contact(item["key"], item["session_date"], "Text Message Sent")
        confirm.assert_called_once_with("VISIT1", "Text Message Sent", expected_campus="City", expected_appointment_date="2026-09-10", expected_appointment_time="10:00:00")
        self.assertEqual(result["contact_status"], "Text Message Sent")

    def test_visit_only_digest_is_queued_and_completed_visit_is_excluded(self):
        fake = TestFollowupDigest().fake()
        with patch.object(service, "frappe", fake), patch.object(service, "collect_followups", return_value=[{"kind": "visit", "pending": True}, {"kind": "visit", "pending": False}]), patch.object(service, "sendmail_or_skip", return_value=None) as mail:
            self.assertEqual(service.run_digest(datetime(2026, 9, 9, 10)), {"queued": True, "count": 1})
        self.assertIn("0 位试课学生、1 个参观预约和 0 位单人补课学生", mail.call_args.kwargs["message"])
