import frappe
from frappe import _
from frappe.model.document import Document


class Teacher(Document):
	def validate(self):
		self.teacher_name = (self.teacher_name or "").strip()
		self.user = (self.user or "").strip()
		if not self.user:
			return
		duplicate = frappe.db.exists(
			"Teacher",
			{
				"user": self.user,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(_("User {0} is already linked to Teacher {1}.").format(self.user, duplicate))
