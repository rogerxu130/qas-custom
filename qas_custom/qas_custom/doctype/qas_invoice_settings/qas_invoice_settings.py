from frappe.model.document import Document
from qas_custom.modules.billing.invoice_settings import validate_reminder_interval


class QASInvoiceSettings(Document):
	def validate(self):
		self.overdue_reminder_interval_days = validate_reminder_interval(self.overdue_reminder_interval_days)
