from frappe.model.document import Document

from qas_custom.modules.payg.pricing import discount_percent


class QASPAYGPricingSettings(Document):
    def validate(self):
        self.discount_percent = discount_percent(self.discount_percent)
