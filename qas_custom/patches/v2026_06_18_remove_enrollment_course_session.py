import frappe


def execute():
	if not frappe.db.table_exists("Enrollment"):
		return
	if not frappe.db.has_column("Enrollment", "course_session"):
		return
	if not frappe.db.has_column("Enrollment", "start_course_session"):
		return

	frappe.db.sql(
		"""
		update `tabEnrollment`
		set start_course_session = course_session
		where coalesce(start_course_session, '') = ''
		  and coalesce(course_session, '') != ''
		"""
	)
