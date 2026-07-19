from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.services.campus_admin import (
	_get_campus_admin_course_session_access,
	get_campus_admin_course_session_data,
	get_campus_admin_course_sessions_data,
	get_campus_admin_session_photo_content_data,
	get_campus_admin_session_video_content_data,
)


class TestCampusAdminReadonlyClasses(TestCase):
	@patch("qas_custom.services.campus_admin._get_course_session_rows")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_session_list_queries_each_assigned_campus_and_merges_in_date_order(self, _labels, require_profile, get_rows):
		require_profile.return_value = {"campuses": ["Campus B", "Campus A"]}
		get_rows.side_effect = [
			[{"name": "SESSION-B", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "15:30:00", "campus": "Campus B"}}],
			[{"name": "SESSION-A", "session_date": "2026-07-18", "weekly_timeslot_detail": {"start_time": "09:00:00", "campus": "Campus A"}}],
		]

		result = get_campus_admin_course_sessions_data(from_date="2026-07-17", to_date="2026-07-31")

		self.assertEqual([item["name"] for item in result["items"]], ["SESSION-A", "SESSION-B"])
		self.assertEqual(
			get_rows.call_args_list,
			[
				call(term=None, course=None, campus="Campus B", from_date="2026-07-17", to_date="2026-07-31", include_inactive_terms=0, include_inactive_timeslots=0, limit=160),
				call(term=None, course=None, campus="Campus A", from_date="2026-07-17", to_date="2026-07-31", include_inactive_terms=0, include_inactive_timeslots=0, limit=160),
			],
		)

	@patch("qas_custom.services.campus_admin._get_course_session_rows")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin.frappe.throw", side_effect=frappe.PermissionError)
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_session_list_rejects_unassigned_requested_campus(self, _labels, _throw, require_profile, get_rows):
		require_profile.return_value = {"campuses": ["Campus A"]}

		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_course_sessions_data(campus="Campus B")

		get_rows.assert_not_called()

	@patch("qas_custom.services.campus_admin.frappe.get_doc")
	@patch("qas_custom.services.campus_admin.frappe.throw", side_effect=frappe.PermissionError)
	def test_session_access_rejects_cross_campus_detail(self, _throw, get_doc):
		session = Mock()
		session.get.return_value = "TIMESLOT-1"
		timeslot = Mock()
		timeslot.get.return_value = "Campus B"
		get_doc.side_effect = [session, timeslot]

		with self.assertRaises(frappe.PermissionError):
			_get_campus_admin_course_session_access("SESSION-1", ["Campus A"])

	@patch("qas_custom.services.campus_admin._count_leave_attendance_rows", return_value=1)
	@patch("qas_custom.services.campus_admin._roster_course_session_attendance_rows")
	@patch("qas_custom.services.campus_admin._visible_course_session_attendance_rows")
	@patch("qas_custom.services.campus_admin._get_school_admin_attendance_rows")
	@patch("qas_custom.services.campus_admin._get_timeslot_summary")
	@patch("qas_custom.services.campus_admin._get_school_admin_session_content_rows")
	@patch("qas_custom.services.campus_admin._document_payload")
	@patch("qas_custom.services.campus_admin._get_campus_admin_course_session_access")
	@patch("qas_custom.services.campus_admin._require_campus_admin_profile")
	@patch("qas_custom.services.campus_admin._attach_campus_admin_teacher_labels")
	def test_allowed_session_detail_returns_read_only_roster_payload(
		self,
		_labels,
		require_profile,
		get_access,
		document_payload,
		get_content,
		get_timeslot,
		get_attendance,
		visible_rows,
		roster_rows,
		_count_leave,
	):
		require_profile.return_value = {"campuses": ["Campus A"]}
		session = SimpleNamespace(name="SESSION-1")
		timeslot = SimpleNamespace(name="TIMESLOT-1")
		get_access.return_value = (session, timeslot)
		document_payload.return_value = {"name": "SESSION-1", "teacher_override": ""}
		get_timeslot.return_value = {"term": "TERM-1", "teacher": "TEACHER-1", "campus": "Campus A"}
		attendance = [
			{"name": "ATT-1", "source_doctype": "Inquiry", "parent_phone": "0400", "has_outstanding_invoice": True},
			{"name": "ATT-2", "status": "Leave"},
		]
		get_attendance.return_value = attendance
		visible_rows.return_value = [attendance[0]]
		roster_rows.return_value = attendance

		payload = get_campus_admin_course_session_data("SESSION-1")

		self.assertEqual(payload["teacher"], "TEACHER-1")
		self.assertEqual(payload["student_count"], 1)
		self.assertEqual(payload["trial_count"], 1)
		self.assertEqual(payload["leave_count"], 1)
		self.assertEqual(payload["attendance"], attendance)
		self.assertEqual(payload["class_content"], get_content.return_value)
		get_content.assert_called_once_with(
			"SESSION-1",
			photo_method="qas_custom.api.campus_admin.campus_admin_get_course_session_photo",
			video_method="qas_custom.api.campus_admin.campus_admin_get_course_session_video",
		)

	@patch("qas_custom.services.campus_admin._get_school_admin_file_content")
	@patch("qas_custom.services.campus_admin.frappe.get_doc")
	@patch("qas_custom.services.campus_admin._get_campus_admin_course_session_access")
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Campus A"]},
	)
	def test_photo_content_requires_allowed_session_matching_post_and_published_status(
		self,
		_require_profile,
		get_access,
		get_doc,
		get_file,
	):
		photo_row = SimpleNamespace(idx=1, image="/private/files/photo.jpg")
		photo_post = Mock()
		photo_post.get.side_effect = lambda field: {
			"course_session": "SESSION-1",
			"status": "Published",
		}.get(field)
		photo_post.photos = [photo_row]
		get_doc.return_value = photo_post
		get_file.return_value = {"filename": "photo.jpg", "content": b"photo", "content_type": "image/jpeg"}

		payload = get_campus_admin_session_photo_content_data("SESSION-1", "PHOTO-1", 1)

		self.assertEqual(payload["filename"], "photo.jpg")
		get_access.assert_called_once_with("SESSION-1", ["Campus A"])
		photo_post.get.side_effect = lambda field: {
			"course_session": "SESSION-2",
			"status": "Published",
		}.get(field)
		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_session_photo_content_data("SESSION-1", "PHOTO-1", 1)

	@patch("qas_custom.services.campus_admin._get_school_admin_file_content")
	@patch("qas_custom.services.campus_admin.frappe.get_doc")
	@patch("qas_custom.services.campus_admin._get_campus_admin_course_session_access")
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Campus A"]},
	)
	def test_video_content_is_inline_and_rejects_drafts(
		self,
		_require_profile,
		get_access,
		get_doc,
		get_file,
	):
		video_post = Mock()
		video_post.get.side_effect = lambda field: {
			"course_session": "SESSION-1",
			"status": "Published",
			"video": "/private/files/video.mp4",
			"file_name": "video.mp4",
			"mime_type": "video/mp4",
		}.get(field)
		get_doc.return_value = video_post
		get_file.return_value = {"filename": "video.mp4", "content": b"video", "content_type": "video/mp4"}

		payload = get_campus_admin_session_video_content_data("SESSION-1", "VIDEO-1")

		self.assertEqual(payload["display_content_as"], "inline")
		get_access.assert_called_once_with("SESSION-1", ["Campus A"])
		video_post.get.side_effect = lambda field: {
			"course_session": "SESSION-1",
			"status": "Draft",
			"video": "/private/files/video.mp4",
		}.get(field)
		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_session_video_content_data("SESSION-1", "VIDEO-1")

	@patch("qas_custom.services.campus_admin.frappe.get_doc")
	@patch(
		"qas_custom.services.campus_admin._get_campus_admin_course_session_access",
		side_effect=frappe.PermissionError,
	)
	@patch(
		"qas_custom.services.campus_admin._require_campus_admin_profile",
		return_value={"campuses": ["Campus A"]},
	)
	def test_media_requests_reject_cross_campus_session_before_loading_posts(
		self,
		_require_profile,
		_get_access,
		get_doc,
	):
		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_session_photo_content_data("SESSION-B", "PHOTO-1", 1)
		with self.assertRaises(frappe.PermissionError):
			get_campus_admin_session_video_content_data("SESSION-B", "VIDEO-1")

		get_doc.assert_not_called()
