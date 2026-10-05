import frappe
from frappe import _
from frappe.model.document import Document


class Enrollment(Document):
	def validate(self):
		self._validate_full_term_fields()

	def on_update(self):
		if self.student and self.status in ("Active", "Planned") and frappe.get_meta("Student").has_field("status"):
			frappe.db.set_value("Student", self.student, "status", "Active")

	def _validate_full_term_fields(self):
		if self.enrollment_type != "Full-Term":
			return

		missing = []
		for fieldname, label in (
			("term", _("Term")),
			("course", _("Course")),
			("weekly_timeslot", _("Weekly Timeslot")),
		):
			if not self.get(fieldname):
				missing.append(label)

		if missing:
			frappe.throw(
				_("Full-Term Enrollment requires: {0}.").format(", ".join(missing))
			)
