from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class WorkshopPhotoPost(Document):
	def validate(self):
		teacher = frappe.db.get_value("Workshop Session", self.workshop_session, "teacher")
		if not teacher or self.teacher != teacher:
			frappe.throw(_("Workshop content teacher must match the assigned Session teacher."))
		if self.status == "Published" and not self.get("photos"):
			frappe.throw(_("Add at least one photo before publishing."))
		if self.status == "Published" and not self.posted_at:
			self.posted_at = now_datetime()
