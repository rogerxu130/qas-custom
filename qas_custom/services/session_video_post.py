import frappe
from frappe import _
from frappe.utils import now_datetime


def validate_session_video_post(doc, method=None):
	if not doc.status:
		doc.status = "Draft"

	if not doc.title:
		doc.title = "Class Video"

	if doc.status == "Published":
		if not doc.course_session:
			frappe.throw(_("Course session is required before publishing class video."))

		if not doc.video:
			frappe.throw(_("A video file is required before publishing class video."))

		if not doc.posted_at:
			doc.posted_at = now_datetime()
