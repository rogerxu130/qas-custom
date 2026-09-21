import frappe
from frappe import _
from frappe.model.document import Document


class WeeklyTimeslot(Document):
	def validate(self):
		from qas_custom.services.class_record_labels import build_weekly_timeslot_label
		self.display_label = build_weekly_timeslot_label(self)

	def before_insert(self):
		if self.term and frappe.db.get_value("Term", self.term, "status") == "Archived":
			frappe.throw(_("Weekly timeslots cannot be added to an archived term. Choose another term."))
