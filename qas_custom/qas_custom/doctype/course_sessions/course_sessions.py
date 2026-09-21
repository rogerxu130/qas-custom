from frappe.model.document import Document


class CourseSessions(Document):
	def validate(self):
		from qas_custom.services.class_record_labels import build_course_session_label
		from qas_custom.services.concentrated_makeup import validate_configuration
		self.display_label = build_course_session_label(self)
		validate_configuration(self)
