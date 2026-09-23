"""PAYG attendance must prove actual participation before family media access."""
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace
from datetime import datetime

import frappe

from qas_custom.services import parent_classroom_messages, parent_feed, teacher_portal
from qas_custom.api import parent_portal


class TestPaygContentAccess(TestCase):
    def _attendance(self, status, booking_status, *, guardian=True, source="QAS PAYG Booking", document="B-1", finished=True):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status=status, source_doctype=source, source_document=document)
        booking = (frappe._dict(name="B-1", attendance_entry="ATT-1", student="S-1",
                                course_session="CS-1", status=booking_status)
                   if booking_status else None)
        fake = SimpleNamespace(get_all=lambda *a, **k: (["S-1"] if guardian else ["S-OTHER"])
                               if a[0] == "Student" else ([row] if guardian else [])
                               if a[0] == "Class Attendance Entry" else ([booking] if booking else []),
                               PermissionError=PermissionError)
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_payg_finished_sessions", return_value={"CS-1"} if finished else set()):
            return parent_feed._accessible_parent_attendance("P-1", "CS-1")

    def test_completed_present_booking_grants_access_after_card_changes(self):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status="Present", source_doctype="QAS PAYG Booking", source_document="B-1")
        booking = frappe._dict(name="B-1", attendance_entry="ATT-1", student="S-1",
                               course_session="CS-1", status="Locked")
        fake = SimpleNamespace(get_all=lambda *a, **k: ["S-1"] if a[0] == "Student" else [row]
                               if a[0] == "Class Attendance Entry" else [booking])
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_payg_finished_sessions", return_value={"CS-1"}):
            self.assertEqual(parent_feed._accessible_parent_attendance("P-1", "CS-1"), [row])

    def test_cancelled_or_unbooked_payg_does_not_grant_access(self):
        row = frappe._dict(name="ATT-1", student="S-1", course_session="CS-1",
                           status="Present", source_doctype="QAS PAYG Booking")
        fake = SimpleNamespace(get_all=lambda *a, **k: ["S-1"] if a[0] == "Student" else [row]
                               if a[0] == "Class Attendance Entry" else [])
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_payg_finished_sessions", return_value=set()):
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
        fake = SimpleNamespace(get_doc=Mock(return_value=frappe._dict(course_session="CS-1", status="Published")),
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

    def test_feed_never_returns_raw_photo_video_or_homework_url(self):
        photo = parent_feed._build_photo_asset("POST-1", {"idx": 1, "image": "/files/public.jpg"})
        self.assertIn("parent_portal_get_feed_photo", photo)
        video = parent_feed._build_video_asset(frappe._dict(name="VIDEO-1", video="https://cdn.example/video.mp4",
                                                             file_name="video.mp4", file_size=1))
        self.assertIn("parent_portal_get_feed_video", video["url"])
        self.assertIn("parent_portal_get_feed_video", video["download_url"])
        homework = parent_feed._build_homework_attachments("HW-1", "/private/files/homework.pdf")
        self.assertIn("parent_portal_get_feed_homework", homework[0]["url"])

    def test_future_present_payg_does_not_grant_access(self):
        self.assertEqual(self._attendance("Present", "Locked", finished=False), [])

    def test_end_time_is_batched_and_future_class_denied(self):
        rows = [frappe._dict(name="CS-PAST", session_date="2026-09-23", weekly_timeslot="W-1"),
                frappe._dict(name="CS-FUTURE", session_date="2026-09-25", weekly_timeslot="W-1")]
        slots = [frappe._dict(name="W-1", start_time="14:00:00", end_time="16:00:00")]
        get_all = Mock(side_effect=[rows, slots])
        with patch.object(parent_feed, "frappe", SimpleNamespace(get_all=get_all)), \
             patch.object(parent_feed, "get_datetime_in_timezone", return_value=datetime(2026, 9, 24, 12)):
            finished = parent_feed._payg_finished_sessions({"CS-PAST", "CS-FUTURE"})
        self.assertEqual(finished, {"CS-PAST"})
        self.assertEqual(get_all.call_count, 2)

    def test_media_external_redirect_uses_current_doc_url_only(self):
        fake = SimpleNamespace(get_doc=Mock(return_value=frappe._dict(course_session="CS-1",
                             status="Published", video="https://cdn.example/file.mp4", file_name="clip.mp4", mime_type="video/mp4")))
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_require_parent", return_value="P-1"), \
             patch.object(parent_feed, "_validate_parent_session_access"):
            result = parent_feed.get_parent_feed_video_content("VIDEO-1")
        self.assertEqual(result["redirect"], "https://cdn.example/file.mp4")

    def test_local_file_lookup_and_homework_authorization(self):
        file_doc = frappe._dict(file_name="homework.pdf")
        file_doc.get_content = Mock(return_value=b"pdf")
        def get_doc(doctype, name):
            if doctype == "Session Homework":
                return frappe._dict(course_session="CS-1", status="Published", attachments="/private/files/homework.pdf")
            if doctype == "File" and name == "FILE-1":
                return file_doc
            raise AssertionError((doctype, name))
        db = SimpleNamespace(get_value=Mock(return_value="FILE-1"))
        fake = SimpleNamespace(get_doc=get_doc, db=db, PermissionError=PermissionError)
        with patch.object(parent_feed, "frappe", fake), \
             patch.object(parent_feed, "_require_parent", return_value="P-1"), \
             patch.object(parent_feed, "_validate_parent_session_access") as authorize:
            payload = parent_feed.get_parent_feed_homework_content("HW-1")
        authorize.assert_called_once_with("P-1", "CS-1")
        db.get_value.assert_called_once_with("File", {"file_url": "/private/files/homework.pdf"}, "name")
        self.assertEqual((payload["content"], payload["display_content_as"]), (b"pdf", "attachment"))

    def test_parent_portal_uses_redirect_only_after_service_check(self):
        frappe.local.flags = frappe._dict(in_test=False)
        response = frappe._dict()
        with patch.object(parent_portal, "get_parent_feed_video_content",
                          return_value={"redirect": "https://cdn.example/file.mp4"}), \
             patch.object(parent_portal.frappe.local, "response", response, create=True):
            parent_portal.parent_portal_get_feed_video("VIDEO-1")
        self.assertEqual((response.type, response.location), ("redirect", "https://cdn.example/file.mp4"))

    def test_draft_media_ids_are_denied_even_for_attending_parent(self):
        for kind, doc in (
            ("photo", frappe._dict(course_session="CS-1", status="Draft",
                                   photos=[frappe._dict(idx=1, image="/private/files/photo.jpg")])),
            ("video", frappe._dict(course_session="CS-1", status="Draft",
                                   video="https://cdn.example/draft.mp4")),
            ("homework", frappe._dict(course_session="CS-1", status="Draft",
                                      attachments="/private/files/draft.pdf")),
        ):
            fake = SimpleNamespace(get_doc=Mock(return_value=doc), PermissionError=PermissionError)
            with self.subTest(kind=kind), patch.object(parent_feed, "frappe", fake), \
                 patch.object(parent_feed, "_require_parent", return_value="P-1"), \
                 patch.object(parent_feed, "_validate_parent_session_access"), \
                 patch.object(parent_feed, "_authorized_media") as media:
                with self.assertRaises(PermissionError):
                    if kind == "photo":
                        parent_feed.get_parent_feed_photo_content("PHOTO-DRAFT", 1)
                    elif kind == "video":
                        parent_feed.get_parent_feed_video_content("VIDEO-DRAFT")
                    else:
                        parent_feed.get_parent_feed_homework_content("HOMEWORK-DRAFT")
                media.assert_not_called()

    def test_published_media_ids_reauthorize_then_resolve_saved_url(self):
        for kind, doc, expected_url in (
            ("photo", frappe._dict(course_session="CS-1", status="Published",
                                   photos=[frappe._dict(idx=1, image="/files/photo.jpg")]), "/files/photo.jpg"),
            ("video", frappe._dict(course_session="CS-1", status="Published", video="https://cdn.example/video.mp4",
                                   file_name="video.mp4", mime_type="video/mp4"), "https://cdn.example/video.mp4"),
            ("homework", frappe._dict(course_session="CS-1", status="Published",
                                      attachments="/private/files/homework.pdf"), "/private/files/homework.pdf"),
        ):
            fake = SimpleNamespace(get_doc=Mock(return_value=doc), PermissionError=PermissionError)
            with self.subTest(kind=kind), patch.object(parent_feed, "frappe", fake), \
                 patch.object(parent_feed, "_require_parent", return_value="P-1"), \
                 patch.object(parent_feed, "_validate_parent_session_access") as authorize, \
                 patch.object(parent_feed, "_authorized_media", return_value={"ok": True}) as media:
                if kind == "photo":
                    result = parent_feed.get_parent_feed_photo_content("PHOTO-PUBLISHED", 1)
                elif kind == "video":
                    result = parent_feed.get_parent_feed_video_content("VIDEO-PUBLISHED")
                else:
                    result = parent_feed.get_parent_feed_homework_content("HOMEWORK-PUBLISHED")
                self.assertEqual(result, {"ok": True})
                authorize.assert_called_once_with("P-1", "CS-1")
                self.assertEqual(media.call_args.args[0], expected_url)
