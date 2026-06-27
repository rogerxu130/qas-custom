import frappe
from frappe import _
from frappe.model.document import Document


class Enrollment(Document):
	def validate(self):
		self._validate_full_term_fields()

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
