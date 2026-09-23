"""PAYG attendance must prove actual participation before family media access."""
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace

import frappe

from qas_custom.services import parent_classroom_messages, parent_feed, teacher_portal


class TestPaygContentAccess(TestCase):
    def _attendance(self, status, booking_status, *, guardian=True, source="QAS PAYG Booking", document="B-1"):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status=status, source_doctype=source, source_document=document)
        booking = (frappe._dict(name="B-1", attendance_entry="ATT-1", student="S-1",
                                course_session="CS-1", status=booking_status)
                   if booking_status else None)
        fake = SimpleNamespace(get_all=lambda *a, **k: (["S-1"] if guardian else ["S-OTHER"])
                               if a[0] == "Student" else ([row] if guardian else [])
                               if a[0] == "Class Attendance Entry" else ([booking] if booking else []),
                               PermissionError=PermissionError)
        with patch.object(parent_feed, "frappe", fake):
            return parent_feed._accessible_parent_attendance("P-1", "CS-1")

    def test_completed_present_booking_grants_access_after_card_changes(self):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status="Present", source_doctype="QAS PAYG Booking", source_document="B-1")
        booking = frappe._dict(name="B-1", attendance_entry="ATT-1", student="S-1",
                               course_session="CS-1", status="Locked")
        fake = SimpleNamespace(get_all=lambda *a, **k: ["S-1"] if a[0] == "Student" else [row]
                               if a[0] == "Class Attendance Entry" else [booking])
        with patch.object(parent_feed, "frappe", fake):
            self.assertEqual(parent_feed._accessible_parent_attendance("P-1", "CS-1"), [row])

    def test_cancelled_or_unbooked_payg_does_not_grant_access(self):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status="Present", source_doctype="QAS PAYG Booking")
        fake = SimpleNamespace(get_all=lambda *a, **k: ["S-1"] if a[0] == "Student" else [row]
                               if a[0] == "Class Attendance Entry" else [])
        with patch.object(parent_feed, "frappe", fake):
            self.assertEqual(parent_feed._accessible_parent_attendance("P-1", "CS-1"), [])

    def test_source_label_does_not_override_booking_or_family(self):
        self.assertEqual(self._attendance("Present", "Cancelled"), [])
        self.assertEqual(self._attendance("Present", "Cancelled", source="Enrollment"), [])
        self.assertEqual(self._attendance("Present", "Locked", document="B-OTHER"), [])
        self.assertEqual(self._attendance("Leave", "Locked"), [])
        self.assertEqual(self._attendance("Present", "Locked", guardian=False), [])
        self.assertEqual(self._attendance("Present", "Locked", source="Enrollment"), [])

    def test_old_trial_attendance_remains_accessible(self):
        self.assertEqual(len(self._attendance("Present", None, source="Trial Inquiry")), 1)

    def test_media_download_checks_session_again(self):
        fake = SimpleNamespace(get_doc=Mock(return_value=frappe._dict(course_session="CS-1")),
                               PermissionError=PermissionError)
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_require_parent", return_value="P-1"), \
             patch.object(parent_feed, "_validate_parent_session_access", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                parent_feed.get_parent_feed_photo_content("PHOTO-1", 1)
            with self.assertRaises(PermissionError):
                parent_feed.get_parent_feed_video_content("VIDEO-1")
        self.assertEqual(fake.get_doc.call_count, 2)

    def test_private_messages_use_the_same_attendance_decision(self):
        row = frappe._dict(name="ATT-1")
        message = frappe._dict(name="MSG-1", student="S-1", attendance_entry="ATT-1")
        get_all = Mock(return_value=[message])
        fake = SimpleNamespace(get_all=get_all)
        with patch.object(parent_classroom_messages, "frappe", fake), \
             patch("qas_custom.services.adhoc_booking.require_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(parent_feed, "_accessible_parent_attendance", return_value=[]):
            self.assertEqual(parent_classroom_messages.get_parent_classroom_messages_data(), {"items": []})
            get_all.assert_not_called()
        with patch.object(parent_classroom_messages, "frappe", fake), \
             patch("qas_custom.services.adhoc_booking.require_parent", return_value=frappe._dict(name="P-1")), \
             patch.object(parent_feed, "_accessible_parent_attendance", return_value=[row]):
            self.assertEqual(parent_classroom_messages.get_parent_classroom_messages_data()["items"], [message])
        self.assertEqual(get_all.call_args.kwargs["filters"]["attendance_entry"], ["in", ["ATT-1"]])

    def test_teacher_counts_payg_source_without_enrollment(self):
        rows = [{"enrollment_type": "Pay-as-you-go", "source_doctype": "QAS PAYG Booking"}]
        self.assertEqual(teacher_portal._count_special_students(rows)["pay_as_you_go"], 1)
