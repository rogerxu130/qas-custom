import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class WorkshopHomework(Document):
	def validate(self):
		teacher = frappe.db.get_value("Workshop Session", self.workshop_session, "teacher")
		if not teacher or self.teacher != teacher:
			frappe.throw(_("Workshop content teacher must match the assigned Session teacher."))
		if self.status == "Published" and not self.published_at:
			self.published_at = now_datetime()
