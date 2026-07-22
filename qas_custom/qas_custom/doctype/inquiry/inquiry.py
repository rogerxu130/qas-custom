from frappe.model.document import Document


class Inquiry(Document):
	def validate(self):
		from qas_custom.services.inquiry import sync_inquiry_course_session

		sync_inquiry_course_session(self)

	def after_insert(self):
		from qas_custom.modules.notifications.commands import enqueue_session_staff_notification
		from qas_custom.modules.notifications.inquiry_admin_notifications import queue_inquiry_admin_notification
		from qas_custom.modules.notifications.trial_parent_notifications import queue_trial_parent_booking_change
		from qas_custom.services.inquiry import ensure_inquiry_attendance_entry

		ensure_inquiry_attendance_entry(self)
		queue_trial_parent_booking_change(self)
		queue_inquiry_admin_notification(self)
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
		from qas_custom.modules.notifications.trial_parent_notifications import queue_trial_parent_booking_change

		old_doc = self.get_doc_before_save()
		old_course_session = old_doc.get("course_session") if old_doc else None
		old_status = old_doc.get("status") if old_doc else None
		queue_trial_parent_booking_change(self, old_doc=old_doc)
		if self.inquiry_type != "Trial Lesson" or not self.student:
			return
		if self.status == "Cancelled" and old_status != "Cancelled" and old_course_session:
			enqueue_session_staff_notification(
				"trial_cancelled",
				course_session=old_course_session,
				student=self.student,
				source_doctype="Inquiry",
				source_document=self.name,
			)
		elif self.course_session and old_course_session != self.course_session:
			if old_course_session:
				enqueue_session_staff_notification(
					"trial_cancelled",
					course_session=old_course_session,
					student=self.student,
					source_doctype="Inquiry",
					source_document=self.name,
				)
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
