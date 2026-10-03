from frappe.model.document import Document
from qas_custom.services.inquiry_parking import validate_wait_days


class QASInquirySettings(Document):
    def validate(self):
        self.parked_wait_days = validate_wait_days(self.parked_wait_days)
