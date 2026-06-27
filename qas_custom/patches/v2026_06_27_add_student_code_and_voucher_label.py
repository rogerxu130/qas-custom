from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

import frappe
from frappe.utils import get_time, getdate


def execute():
	ensure_student_code_field()
	ensure_voucher_label_field()
	backfill_student_codes()
	backfill_voucher_labels()


def ensure_student_code_field():
	if frappe.db.exists("Custom Field", {"dt": "Student", "fieldname": "student_code"}):
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Student",
			"fieldname": "student_code",
			"fieldtype": "Data",
			"label": "Student Code",
			"insert_after": "student_name",
			"in_list_view": 1,
			"in_standard_filter": 1,
			"read_only": 1,
			"description": "Short display code used in portals, for example Isabella01.",
		}
	).insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Student")


def ensure_voucher_label_field():
	if not frappe.db.exists("DocType", "Makeup Voucher"):
		return
	if frappe.db.exists("Custom Field", {"dt": "Makeup Voucher", "fieldname": "voucher_label"}):
		return

	insert_after = "name"
	for fieldname in ("original_session", "course", "student"):
		if frappe.db.exists("DocField", {"parent": "Makeup Voucher", "fieldname": fieldname}):
			insert_after = fieldname
			break

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Makeup Voucher",
			"fieldname": "voucher_label",
			"fieldtype": "Data",
			"label": "Voucher Label",
			"insert_after": insert_after,
			"in_list_view": 1,
			"in_standard_filter": 1,
			"read_only": 1,
			"description": "Readable display label shown in portals instead of the raw voucher id.",
		}
	).insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Makeup Voucher")


def backfill_student_codes():
	if not frappe.db.has_column("Student", "student_code"):
		return

	fields = ["name", "student_name", "guardian"]
	if frappe.db.has_column("Student", "date_of_birth"):
		fields.append("date_of_birth")
	rows = frappe.get_all("Student", fields=fields, order_by="guardian asc, student_name asc, name asc")

	groups = defaultdict(list)
	for row in rows:
		base = _code_base(row.get("student_name") or row.name)
		groups[(row.get("guardian") or "", base)].append(row)

	for (_, base), group in groups.items():
		group.sort(key=lambda row: (str(row.get("date_of_birth") or ""), row.name))
		for index, row in enumerate(group, start=1):
			code = f"{base}{index:02d}"
			if row.get("student_code") != code:
				frappe.db.set_value("Student", row.name, "student_code", code, update_modified=False)


def backfill_voucher_labels():
	if not frappe.db.table_exists("Makeup Voucher"):
		return
	if not frappe.db.has_column("Makeup Voucher", "voucher_label"):
		return

	fields = ["name", "student", "course", "original_session", "voucher_label"]
	vouchers = frappe.get_all("Makeup Voucher", fields=fields)
	for voucher in vouchers:
		label = build_voucher_label(voucher)
		if label and voucher.get("voucher_label") != label:
			frappe.db.set_value("Makeup Voucher", voucher.name, "voucher_label", label, update_modified=False)


def build_voucher_label(voucher):
	parts = []
	session_label = _session_label(voucher.get("original_session"))
	if session_label:
		parts.append(session_label)

	student_code = frappe.db.get_value("Student", voucher.get("student"), "student_code") if voucher.get("student") else None
	if student_code:
		parts.append(student_code)
	elif voucher.get("student"):
		parts.append(_code_base(voucher.get("student")))

	if voucher.get("course"):
		parts.append(voucher.get("course"))
	return " · ".join(parts) or voucher.get("name")


def _session_label(course_session):
	if not course_session or not frappe.db.exists("Course Sessions", course_session):
		return None

	session = frappe.db.get_value("Course Sessions", course_session, ["session_date", "weekly_timeslot"], as_dict=True)
	if not session:
		return None

	parts = []
	if session.get("session_date"):
		parts.append(getdate(session.get("session_date")).strftime("%d %b %Y").lstrip("0"))
	if session.get("weekly_timeslot"):
		start_time = frappe.db.get_value("Weekly Timeslot", session.get("weekly_timeslot"), "start_time")
		if start_time:
			parts.append(datetime.combine(getdate(), get_time(start_time)).strftime("%I:%M %p").lstrip("0"))
	return " ".join(parts)


def _code_base(value):
	base = re.sub(r"[^A-Za-z0-9]+", "", (value or "Student").strip().split(" ")[0])
	return base or "Student"
