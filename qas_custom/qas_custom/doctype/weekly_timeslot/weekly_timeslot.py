import frappe
from frappe import _
from frappe.model.document import Document


class WeeklyTimeslot(Document):
	def before_insert(self):
		if self.term and frappe.db.get_value("Term", self.term, "status") == "Archived":
			frappe.throw(_("Weekly timeslots cannot be added to an archived term. Choose another term."))
