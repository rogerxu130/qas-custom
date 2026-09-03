from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today


class WorkshopEnrollment(Document):
	def before_insert(self):
		offering = frappe.db.get_value("Workshop Offering", self.workshop_offering, ["standard_price", "participation_mode"], as_dict=True)
		if not offering:
			frappe.throw(_("Workshop Offering was not found."))
		self.standard_price_snapshot = flt(offering.standard_price)
		self.enrollment_date = self.enrollment_date or today()

	def validate(self):
		offering = frappe.db.get_value("Workshop Offering", self.workshop_offering, ["status", "capacity", "participation_mode"], as_dict=True)
		if not offering:
			frappe.throw(_("Workshop Offering was not found."))
		student_parent = frappe.db.get_value("Student", self.student, "guardian")
		if student_parent != self.parent:
			frappe.throw(_("Student must belong to the selected Parent / Family."))
		if self.is_new() and offering.status != "Open":
			frappe.throw(_("Participants can only be registered in an Open Workshop Offering."))
		if offering.participation_mode == "Parent and Child" and not (self.adult_participant_name or "").strip():
			frappe.throw(_("Adult participant name is required for a Parent and Child Workshop."))
		if self.status in {"Planned", "Active"}:
			filters = {"workshop_offering": self.workshop_offering, "student": self.student, "status": ["in", ["Planned", "Active"]]}
			if self.name:
				filters["name"] = ["!=", self.name]
			if frappe.db.exists("Workshop Enrollment", filters):
				frappe.throw(_("This Student already has an open Enrollment for the Workshop Offering."))
			if offering.capacity:
				capacity_filters = {"workshop_offering": self.workshop_offering, "status": ["in", ["Planned", "Active"]]}
				if self.name:
					capacity_filters["name"] = ["!=", self.name]
				if frappe.db.count("Workshop Enrollment", capacity_filters) >= int(offering.capacity):
					frappe.throw(_("This Workshop Offering has reached capacity."))
