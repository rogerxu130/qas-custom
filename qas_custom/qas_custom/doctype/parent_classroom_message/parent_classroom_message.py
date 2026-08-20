from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


IMMUTABLE_FIELDS = (
	"course_session",
	"attendance_entry",
	"student",
	"parent",
	"teacher",
	"category",
	"message",
	"recipient_email",
	"client_request_id",
	"created_by_user",
)


class ParentClassroomMessage(Document):
	def validate(self):
		if self.is_new():
			return
		previous = self.get_doc_before_save()
		if not previous:
			return
		for fieldname in IMMUTABLE_FIELDS:
			if previous.get(fieldname) != self.get(fieldname):
				frappe.throw(_("Sent classroom message content and recipients cannot be changed."))

