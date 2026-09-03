import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class InvoiceDiscountTemplate(Document):
	def validate(self):
		self.template_name = (self.template_name or "").strip()
		self.description = (self.description or "").strip()
		if not self.template_name:
			frappe.throw(_("Template name is required."))
		if not self.description:
			frappe.throw(_("Parent-facing description is required."))
		if self.discount_type not in {"Fixed Amount", "Percentage"}:
			frappe.throw(_("Discount type must be Fixed Amount or Percentage."))
		if flt(self.discount_value) <= 0:
			frappe.throw(_("Discount value must be greater than zero."))
		if self.discount_type == "Percentage" and flt(self.discount_value) > 100:
			frappe.throw(_("Percentage discount cannot exceed 100%."))
		if self.status not in {"Active", "Inactive"}:
			frappe.throw(_("Discount Template status must be Active or Inactive."))
