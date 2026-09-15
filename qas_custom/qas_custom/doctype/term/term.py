from frappe.model.document import Document


class Term(Document):
	def validate(self):
		from qas_custom.services.term_lifecycle import validate_term
		validate_term(self)
