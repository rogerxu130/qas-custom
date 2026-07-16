from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import (
	_get_school_admin_session_content_rows,
	get_school_admin_session_photo_content_data,
	get_school_admin_session_video_content_data,
)


class TestSchoolAdminSessionContent(TestCase):
	@patch("qas_custom.services.school_admin._doctype_available", return_value=True)
	@patch("qas_custom.services.school_admin.frappe.get_all")
	def test_content_rows_include_only_published_records_and_sort_newest_first(self, get_all, _doctype_available):
		def rows_for_doctype(doctype, **kwargs):
			self.assertEqual(kwargs.get("filters", {}).get("status"), "Published") if doctype in {
				"Session Homework", "Session Photo Post", "Session Video Post"
			} else None
			if doctype == "Session Homework":
				return [{
					"name": "UPD-1", "title": "Update", "description": "Worked on colour",
					"published_at": "2026-07-17 16:00:00", "teacher": "TEA-1",
				}]
			if doctype == "Session Photo Post":
				return [{
					"name": "PHOTO-1", "title": "Photos", "caption": "Finished pieces",
					"posted_at": "2026-07-17 17:00:00", "teacher": "TEA-1",
				}]
			if doctype == "Session Photo Item":
				return [{"parent": "PHOTO-1", "idx": 1}, {"parent": "PHOTO-1", "idx": 2}]
			if doctype == "Session Video Post":
				return [{
					"name": "VIDEO-1", "title": "Video", "caption": "Demo",
					"posted_at": "2026-07-17 18:00:00", "teacher": "TEA-1",
					"file_name": "demo.mp4", "file_size": 1234,
				}]
			if doctype == "Teacher":
				return [{"name": "TEA-1", "teacher_name": "Amanda Lee"}]
			return []

		get_all.side_effect = rows_for_doctype
		items = _get_school_admin_session_content_rows("SESSION-1")

		self.assertEqual([item["type"] for item in items], ["video_post", "photo_post", "class_update"])
		self.assertEqual(items[1]["photo_count"], 2)
		self.assertIn("course_session=SESSION-1", items[1]["photos"][0]["url"])
		self.assertEqual(items[0]["teacher_name"], "Amanda Lee")

	@patch("qas_custom.services.school_admin._get_school_admin_file_content")
	@patch("qas_custom.services.school_admin.frappe.get_doc")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_photo_content_requires_matching_session_and_published_status(self, _require, get_doc, get_file):
		photo_row = SimpleNamespace(idx=1, image="/private/files/photo.jpg")
		photo_post = Mock()
		photo_post.get.side_effect = lambda field: {"course_session": "SESSION-1", "status": "Published"}.get(field)
		photo_post.photos = [photo_row]
		get_doc.return_value = photo_post
		get_file.return_value = {"filename": "photo.jpg", "content": b"photo", "content_type": "image/jpeg"}

		payload = get_school_admin_session_photo_content_data("SESSION-1", "PHOTO-1", 1)
		self.assertEqual(payload["filename"], "photo.jpg")

		with self.assertRaises(frappe.PermissionError):
			get_school_admin_session_photo_content_data("SESSION-2", "PHOTO-1", 1)

	@patch("qas_custom.services.school_admin._get_school_admin_file_content")
	@patch("qas_custom.services.school_admin.frappe.get_doc")
	@patch("qas_custom.services.school_admin._require_school_admin")
	def test_video_content_is_inline_and_rejects_drafts(self, _require, get_doc, get_file):
		video_post = Mock()
		video_post.get.side_effect = lambda field: {
			"course_session": "SESSION-1", "status": "Published", "video": "/private/files/video.mp4",
			"file_name": "video.mp4", "mime_type": "video/mp4",
		}.get(field)
		get_doc.return_value = video_post
		get_file.return_value = {"filename": "video.mp4", "content": b"video", "content_type": "video/mp4"}

		payload = get_school_admin_session_video_content_data("SESSION-1", "VIDEO-1")
		self.assertEqual(payload["display_content_as"], "inline")

		video_post.get.side_effect = lambda field: {
			"course_session": "SESSION-1", "status": "Draft", "video": "/private/files/video.mp4"
		}.get(field)
		with self.assertRaises(frappe.PermissionError):
			get_school_admin_session_video_content_data("SESSION-1", "VIDEO-1")
