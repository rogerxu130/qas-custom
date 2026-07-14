from frappe.model.document import Document


class Inquiry(Document):
	def validate(self):
		from qas_custom.services.inquiry import sync_inquiry_course_session

		sync_inquiry_course_session(self)

	def after_insert(self):
		from qas_custom.services.inquiry import ensure_inquiry_attendance_entry
		from qas_custom.modules.notifications.commands import enqueue_session_staff_notification

		ensure_inquiry_attendance_entry(self)
		if self.inquiry_type == "Trial Lesson" and self.course_session and self.student:
			enqueue_session_staff_notification(
				"trial_added",
				course_session=self.course_session,
				student=self.student,
				source_doctype="Inquiry",
				source_document=self.name,
			)

	def on_update(self):
		from qas_custom.modules.notifications.commands import enqueue_session_staff_notification

		old_doc = self.get_doc_before_save()
		old_course_session = old_doc.get("course_session") if old_doc else None
		if (
			self.inquiry_type == "Trial Lesson"
			and self.course_session
			and self.student
			and old_course_session != self.course_session
		):
			enqueue_session_staff_notification(
				"trial_added",
				course_session=self.course_session,
				student=self.student,
				source_doctype="Inquiry",
				source_document=self.name,
			)

	def on_trash(self):
		from qas_custom.modules.attendance.commands import remove_trial_inquiry_attendance_entries

		remove_trial_inquiry_attendance_entries(self.name)
