from frappe.model.document import Document
from qas_custom.modules.billing.invoice_settings import validate_reminder_interval, validate_course_due_days


class QASInvoiceSettings(Document):
	def validate(self):
		self.overdue_reminder_interval_days = validate_reminder_interval(self.overdue_reminder_interval_days)
		for field in ("course_due_lead_days", "course_due_grace_days"):
			self.set(field, validate_course_due_days(self.get(field), field))
