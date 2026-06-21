from __future__ import annotations

import frappe

from qas_custom.services.class_attendance import (
	create_attendance_entry,
	remove_attendance_entries_by_source,
)

PAY_AS_YOU_GO = "Pay-as-you-go"
DEFAULT_ATTENDANCE_STATUS = "To be started"


def add_adhoc_attendance_entry(course_session: str, student: str, booking: str):
	return create_attendance_entry(
		course_session=course_session,
		student=student,
		enrollment_type=PAY_AS_YOU_GO,
		source_doctype="Adhoc Booking",
		source_document=booking,
		status=DEFAULT_ATTENDANCE_STATUS,
		comments=f"Added from Adhoc Booking {booking}",
		prevent_student_duplicate=True,
	)


def remove_adhoc_attendance_for_booking(booking: str):
	return bool(remove_attendance_entries_by_source("Adhoc Booking", booking))
