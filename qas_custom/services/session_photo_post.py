import frappe
from frappe import _
from frappe.utils import now_datetime


def validate_session_photo_post(doc, method=None):
	if not doc.status:
		doc.status = "Draft"

	if not doc.title:
		doc.title = _("Class Photos")

	if not doc.teacher:
		doc.teacher = _get_current_teacher()

	if doc.status != "Published":
		return

	if not doc.course_session:
		frappe.throw(_("Course session is required before publishing class photos."))

	if not _has_uploaded_photos(doc):
		frappe.throw(_("At least one photo is required before publishing class photos."))

	if not doc.posted_at:
		doc.posted_at = now_datetime()


def _has_uploaded_photos(doc):
	return any(getattr(photo, "image", None) for photo in doc.photos or [])


def _get_current_teacher():
	if frappe.session.user in ("Guest", "Administrator"):
		return None

	return frappe.db.get_value("Teacher", {"user": frappe.session.user}, "name")
