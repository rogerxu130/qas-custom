from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class WorkshopAttendance(Document):
	def validate(self):
		if self.status not in {"Not Marked", "Present", "Absent", "Late", "Cancelled"}:
			frappe.throw(_("Invalid Workshop Attendance status."))
		session_offering = frappe.db.get_value("Workshop Session", self.workshop_session, "workshop_offering")
		enrollment = frappe.db.get_value("Workshop Enrollment", self.workshop_enrollment, ["workshop_offering", "student"], as_dict=True)
		if not session_offering or not enrollment:
			frappe.throw(_("Workshop Session and Enrollment are required."))
		if session_offering != enrollment.workshop_offering or self.student != enrollment.student:
			frappe.throw(_("Workshop Attendance Session, Enrollment, and Student do not belong to the same package."))
		filters = {"workshop_session": self.workshop_session, "workshop_enrollment": self.workshop_enrollment}
		if self.name:
			filters["name"] = ["!=", self.name]
		if frappe.db.exists("Workshop Attendance", filters):
			frappe.throw(_("Attendance already exists for this Workshop Session and Enrollment."))
