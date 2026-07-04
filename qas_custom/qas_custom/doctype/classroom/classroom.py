import frappe
import re
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists


class Classroom(Document):
	def autoname(self):
		self.name = _classroom_record_name(self.campus, self.classroom_name)
		if frappe.db.exists("Classroom", self.name):
			self.name = append_number_if_name_exists("Classroom", self.name)

	def validate(self):
		classroom_name = (self.classroom_name or "").strip()
		campus = (self.campus or "").strip()
		self.classroom_name = classroom_name
		self.campus = campus
		if campus and not frappe.db.exists("Campus", campus):
			frappe.throw(_("Campus does not exist: {0}").format(campus))
		if classroom_name and campus:
			duplicate = frappe.db.exists(
				"Classroom",
				{
					"campus": campus,
					"classroom_name": classroom_name,
					"name": ["!=", self.name],
				},
			)
			if duplicate:
				frappe.throw(_("Room {0} already exists at {1}.").format(classroom_name, campus))


def _classroom_record_name(campus, classroom_name):
	return "-".join([_slug_part(campus), _slug_part(classroom_name)])


def _slug_part(value):
	text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
	return text or "room"
