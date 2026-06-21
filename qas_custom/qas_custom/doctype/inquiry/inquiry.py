from frappe.model.document import Document


class Inquiry(Document):
	def validate(self):
		from qas_custom.services.inquiry import sync_inquiry_course_session

		sync_inquiry_course_session(self)

	def after_insert(self):
		from qas_custom.services.inquiry import backfill_inquiry_attendance_source

		backfill_inquiry_attendance_source(self)
