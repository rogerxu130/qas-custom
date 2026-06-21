from __future__ import annotations

import frappe

from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE, create_attendance_entry


def execute():
	if not frappe.db.table_exists(ATTENDANCE_DOCTYPE):
		return
	if not frappe.db.table_exists("Makeup Voucher"):
		return

	vouchers = frappe.get_all(
		"Makeup Voucher",
		filters={
			"status": "Used",
			"used_on_session": ["is", "set"],
			"student": ["is", "set"],
		},
		fields=["name", "student", "used_on_session"],
	)

	for voucher in vouchers:
		if _has_attendance_for_voucher(voucher.name, voucher.used_on_session):
			continue
		if _student_already_listed(voucher.student, voucher.used_on_session):
			continue
		create_attendance_entry(
			course_session=voucher.used_on_session,
			student=voucher.student,
			enrollment_type="Makeup",
			source_doctype="Makeup Voucher",
			source_document=voucher.name,
			comments=f"Added from Makeup Voucher {voucher.name}",
			makeup_voucher=voucher.name,
		)

	frappe.clear_cache(doctype=ATTENDANCE_DOCTYPE)


def _has_attendance_for_voucher(voucher_name, course_session):
	return bool(
		frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{
				"course_session": course_session,
				"source_doctype": "Makeup Voucher",
				"source_document": voucher_name,
			},
		)
	)


def _student_already_listed(student, course_session):
	return bool(
		frappe.db.exists(
			ATTENDANCE_DOCTYPE,
			{
				"course_session": course_session,
				"student": student,
			},
		)
	)
