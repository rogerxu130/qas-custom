from frappe.model.document import Document


class ClassAttendanceEntry(Document):
	def validate(self):
		from qas_custom.services.concentrated_makeup import validate_attendance
		validate_attendance(self)
