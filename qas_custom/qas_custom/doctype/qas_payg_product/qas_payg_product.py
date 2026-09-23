import frappe
from frappe.model.document import Document
from qas_custom.qas_custom.doctype.payg_validation import nonnegative, integer

class QASPAYGProduct(Document):
    def validate(self):
        nonnegative(self, "standard_card_price")
        if integer(self, "sessions_per_card") != 10:
            frappe.throw("PAYG products must contain exactly 10 sessions")
