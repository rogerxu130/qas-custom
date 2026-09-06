from frappe.model.document import Document


class CourseSessions(Document):
	def validate(self):
		from qas_custom.services.concentrated_makeup import validate_configuration
		validate_configuration(self)
