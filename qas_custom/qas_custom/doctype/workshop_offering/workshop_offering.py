from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class WorkshopOffering(Document):
	def validate(self):
		if self.workshop_category not in {"Holiday Camp", "General Workshop"}:
			frappe.throw(_("Workshop category must be Holiday Camp or General Workshop."))
		if self.participation_mode not in {"Individual", "Parent and Child"}:
			frappe.throw(_("Participation mode must be Individual or Parent and Child."))
		if self.class_language not in {"English", "Chinese"}:
			frappe.throw(_("Class language must be English or Chinese."))
		if flt(self.standard_price) < 0:
			frappe.throw(_("Standard price cannot be negative."))
		if cint(self.capacity or 0) < 0:
			frappe.throw(_("Capacity cannot be negative. Leave it blank or zero for no limit."))
		if self.minimum_age is not None and cint(self.minimum_age) < 0:
			frappe.throw(_("Minimum age cannot be negative."))
		if self.maximum_age is not None and cint(self.maximum_age) < cint(self.minimum_age or 0):
			frappe.throw(_("Maximum age cannot be less than minimum age."))
		if not self.is_new() and self.status == "Open":
			if not frappe.db.exists("Workshop Session", {"workshop_offering": self.name, "status": ["!=", "Cancelled"]}):
				frappe.throw(_("Add at least one scheduled Workshop Session before opening this Offering."))
