from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_time


class WorkshopSession(Document):
	def validate(self):
		if not self.start_time or not self.end_time:
			frappe.throw(_("Workshop Session start and end times are required."))
		if get_time(self.end_time) <= get_time(self.start_time):
			frappe.throw(_("Workshop Session end time must be after start time."))
		offering_campus = frappe.db.get_value("Workshop Offering", self.workshop_offering, "campus")
		if not offering_campus:
			frappe.throw(_("Workshop Offering is required."))
		self.campus = offering_campus
		if self.classroom:
			classroom_campus = frappe.db.get_value("Classroom", self.classroom, "campus")
			if classroom_campus and classroom_campus != offering_campus:
				frappe.throw(_("Workshop Session classroom must belong to the Offering campus."))
		filters = {
			"workshop_offering": self.workshop_offering,
			"session_date": self.session_date,
			"start_time": self.start_time,
			"end_time": self.end_time,
		}
		if self.name:
			filters["name"] = ["!=", self.name]
		if frappe.db.exists("Workshop Session", filters):
			frappe.throw(_("This Workshop Session date and time already exists in the Offering."))

	def on_trash(self):
		if frappe.db.exists("Workshop Attendance", {"workshop_session": self.name}):
			frappe.throw(_("A Workshop Session with Attendance cannot be deleted. Cancel it instead."))
		for doctype in ("Workshop Homework", "Workshop Photo Post", "Workshop Video Post"):
			if frappe.db.exists(doctype, {"workshop_session": self.name, "status": "Published"}):
				frappe.throw(_("A Workshop Session with published content cannot be deleted."))
