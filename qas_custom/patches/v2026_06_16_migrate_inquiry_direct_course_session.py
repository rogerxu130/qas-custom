import frappe


def execute():
	if not frappe.db.table_exists("Inquiry"):
		return

	if frappe.db.has_column("Inquiry", "current_course_session") and frappe.db.has_column("Inquiry", "course_session"):
		frappe.db.sql(
			"""
			update `tabInquiry`
			set course_session = current_course_session
			where coalesce(course_session, '') = ''
			  and coalesce(current_course_session, '') != ''
			"""
		)

	if not (
		frappe.db.has_column("Inquiry", "current_appointment")
		and frappe.db.has_column("Inquiry", "attendance_row_id")
		and frappe.db.table_exists("Inquiry Appointment")
	):
		return

	rows = frappe.db.sql(
		"""
		select inquiry.name, appointment.attendance_row_id
		from `tabInquiry` inquiry
		inner join `tabInquiry Appointment` appointment
			on appointment.name = inquiry.current_appointment
		where coalesce(inquiry.attendance_row_id, '') = ''
		  and coalesce(appointment.attendance_row_id, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value("Inquiry", row.name, "attendance_row_id", row.attendance_row_id, update_modified=False)
