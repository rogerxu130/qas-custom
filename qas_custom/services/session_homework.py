import frappe
from frappe import _
from frappe.utils import now_datetime


def validate_session_homework(doc, method=None):
	if not doc.status:
		doc.status = "Draft"

	if not doc.teacher:
		doc.teacher = _get_current_teacher()

	if doc.status != "Published":
		return

	if not doc.course_session:
		frappe.throw(_("Course session is required before publishing homework."))

	if not doc.title:
		frappe.throw(_("Title is required before publishing homework."))

	if not doc.published_at:
		doc.published_at = now_datetime()


def _get_current_teacher():
	if frappe.session.user in ("Guest", "Administrator"):
		return None

	return frappe.db.get_value("Teacher", {"user": frappe.session.user}, "name")
