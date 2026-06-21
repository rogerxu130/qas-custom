from __future__ import annotations

import frappe

from qas_custom.services.attendance_source import set_attendance_row_source

PAY_AS_YOU_GO = "Pay-as-you-go"
DEFAULT_ATTENDANCE_STATUS = "To be started"


def add_adhoc_attendance_row(course_session: str, student: str, booking: str):
	session_doc = frappe.get_doc("Course Sessions", course_session)
	for row in session_doc.get("attendance_list", []):
		if row.student == student and row.enrollment_type == PAY_AS_YOU_GO:
			frappe.throw("This student already has a Pay-as-you-go booking for this session.")
		if row.student == student:
			frappe.throw("This student is already listed for this session.")

	row = session_doc.append(
		"attendance_list",
		{
			"student": student,
			"enrollment_type": PAY_AS_YOU_GO,
			"status": DEFAULT_ATTENDANCE_STATUS,
			"comments": f"Added from Adhoc Booking {booking}",
		},
	)
	set_attendance_row_source(row, "Adhoc Booking", booking)
	session_doc.save(ignore_permissions=True)
	return row.name


def remove_adhoc_attendance_row(course_session: str, attendance_row_id: str | None):
	if not course_session or not attendance_row_id:
		return False

	session_doc = frappe.get_doc("Course Sessions", course_session)
	target = None
	for row in session_doc.get("attendance_list", []):
		if row.name == attendance_row_id and row.enrollment_type == PAY_AS_YOU_GO:
			target = row
			break

	if not target:
		return False

	session_doc.remove(target)
	session_doc.save(ignore_permissions=True)
	return True
