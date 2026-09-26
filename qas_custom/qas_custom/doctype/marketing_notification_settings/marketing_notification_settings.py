from frappe.model.document import Document


class MarketingNotificationSettings(Document):
    def validate(self):
        from qas_custom.services.marketing_notifications import validate_settings
        from qas_custom.services.support_view import reject_support_view_write
        reject_support_view_write()
        self.enabled, self.recipient = validate_settings(self.enabled, self.recipient)
