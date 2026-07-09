from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import re

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from qas_custom.modules.billing.store_credit import LEDGER_DOCTYPE, adjust_store_credit
from qas_custom.services.class_attendance import ATTENDANCE_DOCTYPE
from qas_custom.services.inquiry import (
	create_inquiry_core,
	_derive_campus_and_course,
	_parse_class_session,
	_resolve_campus,
	_resolve_course,
)
from qas_custom.services.parent_customer import ensure_parent_customer
from qas_custom.services.school_admin import _require_school_admin


ACTIVE_STUDENT_STATUS = "Active"
INACTIVE_STUDENT_STATUS = "Inactive"
ACTIVE_PARENT_STATUS = "Active"
INACTIVE_PARENT_STATUS = "Inactive"


def preview_parent_student_import_data(payload=None):
	_require_school_admin()
	batch = _build_import_batch(payload)
	return _preview_batch(batch)


def run_parent_student_import_data(payload=None):
	_require_school_admin()
	batch = _build_import_batch(payload)
	preview = _preview_batch(batch)
	if preview.get("blocking_error_count"):
		return {
			"ok": False,
			"dry_run": False,
			"message": _("Import has blocking errors. Run preview and fix the CSV first."),
			"preview": preview,
		}

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	result["warnings"] = list(preview.get("warnings") or [])
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		for parent_record in batch.get("parents", []):
			savepoint = f"qas_parent_import_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(savepoint)
			try:
				parent_result = _run_parent_record(parent_record)
				result["parents"].append(parent_result)
				_accumulate_counts(result["counts"], parent_result.get("counts") or {})
				result["warnings"].extend(parent_result.get("warnings") or [])
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				error = {
					"parent_email": parent_record.get("email"),
					"row_numbers": parent_record.get("row_numbers"),
					"message": str(exc),
				}
				result["errors"].append(error)
				result["counts"]["parent_errors"] += 1
				frappe.log_error(frappe.get_traceback(), "QAS parent/student import failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	return _finalize_result(result)


def preview_store_credit_import_data(payload=None):
	_require_school_admin()
	batch = _build_store_credit_import_batch(payload)
	return _preview_store_credit_batch(batch)


def run_store_credit_import_data(payload=None):
	_require_school_admin()
	batch = _build_store_credit_import_batch(payload)
	preview = _preview_store_credit_batch(batch)
	if preview.get("blocking_error_count"):
		return {
			"ok": False,
			"dry_run": False,
			"message": _("Store credit import has blocking errors. Run preview and fix the CSV first."),
			"preview": preview,
		}

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	result["warnings"] = list(preview.get("warnings") or [])
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		for row in batch.get("credits", []):
			savepoint = f"qas_store_credit_import_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(savepoint)
			try:
				row_result = _run_store_credit_row(row)
				result["parents"].append(row_result)
				_accumulate_counts(result["counts"], row_result.get("counts") or {})
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result["errors"].append({
					"row": row.get("row_number"),
					"parent_email": row.get("parent_email"),
					"field": "store_credit",
					"message": str(exc),
				})
				result["counts"]["store_credit_errors"] += 1
				frappe.log_error(frappe.get_traceback(), "QAS store credit import failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	return _finalize_result(result)


def preview_trial_inquiry_import_data(payload=None):
	_require_school_admin()
	batch = _build_trial_inquiry_import_batch(payload)
	return _preview_trial_inquiry_batch(batch)


def run_trial_inquiry_import_data(payload=None):
	_require_school_admin()
	batch = _build_trial_inquiry_import_batch(payload)
	preview = _preview_trial_inquiry_batch(batch)
	if preview.get("blocking_error_count"):
		return {
			"ok": False,
			"dry_run": False,
			"message": _("Trial inquiry import has blocking errors. Run preview and fix the CSV first."),
			"preview": preview,
		}

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	result["warnings"] = list(preview.get("warnings") or [])
	previous_in_import = getattr(frappe.flags, "in_import", None)
	frappe.flags.in_import = True
	try:
		for row in batch.get("trials", []):
			savepoint = f"qas_trial_import_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(savepoint)
			try:
				row_result = _run_trial_inquiry_row(row)
				result["parents"].append(row_result)
				_accumulate_counts(result["counts"], row_result.get("counts") or {})
				result["warnings"].extend(row_result.get("warnings") or [])
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result["errors"].append({
					"row": row.get("row_number"),
					"parent_email": row.get("parent_email"),
					"field": "trial_inquiry",
					"message": str(exc),
				})
				result["counts"]["trial_inquiry_errors"] += 1
				frappe.log_error(frappe.get_traceback(), "QAS trial inquiry import failed")
	finally:
		_restore_flag("in_import", previous_in_import)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	return _finalize_result(result)


def preview_enrollment_import_data(payload=None):
	_require_school_admin()
	batch = _build_enrollment_import_batch(payload)
	return _preview_enrollment_batch(batch)


def run_enrollment_import_data(payload=None):
	_require_school_admin()
	batch = _build_enrollment_import_batch(payload)
	preview = _preview_enrollment_batch(batch)
	if preview.get("blocking_error_count"):
		return {
			"ok": False,
			"dry_run": False,
			"message": _("Enrollment import has blocking errors. Run preview and fix the CSV first."),
			"preview": preview,
		}

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	result["warnings"] = list(preview.get("warnings") or [])
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		for row in batch.get("enrollments", []):
			savepoint = f"qas_enrollment_import_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(savepoint)
			try:
				row_result = _run_enrollment_row(row)
				result["parents"].append(row_result)
				_accumulate_counts(result["counts"], row_result.get("counts") or {})
				result["warnings"].extend(row_result.get("warnings") or [])
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result["errors"].append({
					"row": row.get("row_number"),
					"parent_email": row.get("parent_email"),
					"field": "enrollment",
					"message": str(exc),
				})
				result["counts"]["enrollment_errors"] += 1
				frappe.log_error(frappe.get_traceback(), "QAS enrollment import failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	return _finalize_result(result)


def _build_enrollment_import_batch(payload=None):
	payload = _get_payload(payload)
	rows = payload.get("rows") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		frappe.throw(_("Import rows must be a list."))
	default_term = _normalize_spaces(payload.get("default_term")) if isinstance(payload, dict) else ""

	row_results = []
	enrollments = []
	seen_keys = set()
	for index, raw_row in enumerate(rows, start=1):
		if not isinstance(raw_row, dict):
			row_results.append(_row_error(index, None, "row", _("Row must be an object.")))
			continue
		row = _normalize_enrollment_row(raw_row, index, default_term=default_term)
		key = _enrollment_import_key(row)
		if key and key in seen_keys:
			row["duplicate_in_file"] = True
		elif key:
			seen_keys.add(key)
		row_results.append(row)
		if not row.get("errors"):
			enrollments.append(row)
	return {"rows": row_results, "enrollments": enrollments}


def _normalize_enrollment_row(raw_row, row_number, default_term=""):
	row = {str(key or "").strip(): _clean_text(value) for key, value in raw_row.items()}
	enrollment = _enrollment_id_from_row(row)
	student = _normalize_spaces(_first(row, ["student", "Student", "student_id", "Student ID"]))
	parent = _normalize_spaces(_first(row, ["parent", "Parent", "parent_id", "Parent ID"]))
	parent_email = _normalize_email(_first(row, ["parent_email", "Parent Email", "Contact Email", "Contact Email Address", "email", "Email", "linked_user"]))
	parent_name = _normalize_spaces(_first(row, ["parent_name", "Parent Name", "contact_name", "Contact Name"]))
	parent_mobile = _normalize_phone(_first(row, ["parent_mobile", "Parent Mobile", "parent_phone", "Parent Phone", "Mobile", "Phone", "Phone Number", "Phone bumber"]))
	student_name = _normalize_spaces(_first(row, ["student_name", "Student Name", "submitted_student_name", "Name"]))
	student_dob = _parse_date(_first(row, ["student_dob", "Student DOB", "Student Birthday", "Date of Birth", "date_of_birth", "dob", "DOB"]))
	term = _normalize_spaces(_first(row, ["term", "Term"])) or default_term
	course = _normalize_spaces(_first(row, ["course", "Course", "class_type", "Class Type"]))
	campus = _normalize_spaces(_first(row, ["campus", "Campus", "Which Campus?"]))
	form_name = _normalize_spaces(_first(row, ["form_name", "Form Name", "formname", "Formname"]))
	if form_name:
		derived_campus, derived_course = _derive_campus_and_course(form_name)
		campus = campus or derived_campus or ""
		course = course or derived_course or ""
	session_label = _normalize_spaces(_first(row, ["session_label", "class_session", "Class Session", "Which session?", "submitted_class_session"]))
	weekly_timeslot = _normalize_spaces(_first(row, ["weekly_timeslot", "Weekly Timeslot", "timeslot", "Weekly Timeslot ID"]))
	start_course_session = _normalize_spaces(_first(row, ["start_course_session", "Start Course Session", "course_session", "Course Session"]))
	enrollment_type = _normalize_spaces(_first(row, ["enrollment_type", "Enrollment Type"])) or "Full-Term"
	submitted_status = _normalize_spaces(_first(row, ["status", "Status", "enrollment_status", "Enrollment Status"]))
	enrollment_date = _parse_date(_first(row, ["enrollment_date", "Enrollment Date", "created_at", "Creation"]))
	source_id = _normalize_spaces(_first(row, ["source_id", "Source ID", "entry_id", "Entry ID", "ID"]))

	normalized = {
		"row_number": row_number,
		"source": row,
		"source_id": source_id,
		"enrollment": enrollment,
		"student": student,
		"parent": parent,
		"parent_email": parent_email,
		"parent_name": parent_name,
		"parent_mobile": parent_mobile,
		"student_name": student_name,
		"student_dob": student_dob,
		"term": term,
		"course": course,
		"campus": campus,
		"form_name": form_name,
		"session_label": session_label,
		"weekly_timeslot": weekly_timeslot,
		"start_course_session": start_course_session,
		"enrollment_type": enrollment_type,
		"status": "Planned",
		"submitted_status": submitted_status,
		"enrollment_date": enrollment_date,
		"errors": [],
	}
	if enrollment_type != "Full-Term":
		normalized["errors"].append(_field_error("enrollment_type", _("Only Full-Term enrollment import is supported.")))
	if not normalized["term"] and not normalized["weekly_timeslot"]:
		normalized["errors"].append(_field_error("term", _("Term is required unless Weekly Timeslot can provide it.")))
	if not normalized["parent"] and not normalized["parent_email"]:
		normalized["errors"].append(_field_error("parent_email", _("Parent or parent email is required.")))
	if not normalized["student"] and not normalized["student_name"]:
		normalized["errors"].append(_field_error("student_name", _("Student or student name is required.")))
	if not normalized["weekly_timeslot"] and not (normalized["campus"] and normalized["course"] and normalized["session_label"]):
		normalized["errors"].append(_field_error("weekly_timeslot", _("Weekly Timeslot, or campus + course + session_label, is required.")))
	return normalized


def _preview_enrollment_batch(batch):
	result = _empty_result(dry_run=True)
	valid_rows = batch.get("enrollments") or []
	result["input"] = {
		"row_count": len(batch.get("rows") or []),
		"parent_count": len({_enrollment_parent_key(row) for row in valid_rows if _enrollment_parent_key(row)}),
		"student_count": len(valid_rows),
		"enrollment_count": len(valid_rows),
	}

	for row in batch.get("rows") or []:
		for error in row.get("errors") or []:
			result["errors"].append({"row": row.get("row_number"), "parent_email": row.get("parent_email"), **error})

	for row in valid_rows:
		preview = _preview_enrollment_row(row)
		result["parents"].append(preview)
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])

	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_enrollment_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	if row.get("duplicate_in_file"):
		counts["enrollment_duplicates_in_file"] += 1
		warnings.append(_enrollment_issue(row, "enrollment", _("Duplicate row in this CSV. The later duplicate will be skipped.")))
		return _enrollment_preview_payload(row, counts, warnings=warnings, skipped=True)

	if row.get("enrollment"):
		if frappe.db.exists("Enrollment", row.get("enrollment")):
			counts["enrollments_existing_skipped"] += 1
			warnings.append(_enrollment_issue(row, "enrollment", _("Existing Enrollment {0} will be skipped. Use the Enrollment workbench for updates.").format(row.get("enrollment"))))
			return _enrollment_preview_payload(row, counts, warnings=warnings, skipped=True)
		errors.append(_enrollment_issue(row, "enrollment", _("Enrollment ID {0} was not found. Remove it for a new import row.").format(row.get("enrollment"))))
		return _enrollment_preview_payload(row, counts, errors=errors)

	family = _resolve_enrollment_family_preview(row)
	_accumulate_counts(counts, family.get("counts") or {})
	errors.extend(family.get("errors") or [])
	warnings.extend(family.get("warnings") or [])

	schedule = _resolve_enrollment_schedule_preview(row)
	_accumulate_counts(counts, schedule.get("counts") or {})
	errors.extend(schedule.get("errors") or [])
	warnings.extend(schedule.get("warnings") or [])

	if row.get("submitted_status") and row.get("submitted_status") != "Planned":
		warnings.append(_enrollment_issue(row, "status", _("Enrollment import creates Planned records. Submitted status {0} will be ignored.").format(row.get("submitted_status"))))

	student = family.get("student")
	term = schedule.get("term")
	weekly_timeslot = schedule.get("weekly_timeslot")
	if not errors and student and term and weekly_timeslot and _enrollment_duplicate_exists(student, term, weekly_timeslot):
		counts["enrollments_duplicates_skipped"] += 1
		warnings.append(_enrollment_issue(row, "enrollment", _("Matching Planned or Active Enrollment already exists and will be skipped.")))
	elif not errors:
		counts["enrollments_to_create"] += 1
	return _enrollment_preview_payload(row, counts, family=family, schedule=schedule, errors=errors, warnings=warnings)


def _resolve_enrollment_family_preview(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	parent = None
	student = None
	parent_matches = []

	if row.get("parent"):
		if frappe.db.exists("Parent", row.get("parent")):
			parent = row.get("parent")
			counts["parents_reused"] += 1
		else:
			errors.append(_enrollment_issue(row, "parent", _("Parent {0} was not found.").format(row.get("parent"))))
	elif row.get("parent_email"):
		parent_matches = _find_parent_matches(row.get("parent_email"))
		if len(parent_matches) > 1:
			errors.append(_enrollment_issue(row, "parent_email", _("Multiple Parent records match email {0}.").format(row.get("parent_email")), matches=parent_matches))
		elif parent_matches:
			parent = parent_matches[0]
			counts["parents_reused"] += 1
		else:
			counts["parents_to_create"] += 1
			if not row.get("parent_name"):
				errors.append(_enrollment_issue(row, "parent_name", _("Parent name is required when creating a new parent.")))

	if parent and _record_status("Parent", parent) not in ("", ACTIVE_PARENT_STATUS):
		counts["parents_to_update"] += 1

	if row.get("parent_email"):
		if _find_user(row.get("parent_email")):
			counts["users_reused"] += 1
		else:
			counts["users_to_create"] += 1

	customer = _parent_customer(parent) if parent else None
	if parent and not customer:
		customer = _find_customer_for_parent(row.get("parent_email"), [parent])
	if parent:
		counts["customers_reused" if customer else "customers_to_create"] += 1
	elif row.get("parent_email"):
		counts["customers_to_create"] += 1

	if row.get("student"):
		if frappe.db.exists("Student", row.get("student")):
			student = row.get("student")
			counts["students_reused"] += 1
			if parent and not _student_can_belong_to_parent(student, parent):
				errors.append(_enrollment_issue(row, "student", _("Student {0} belongs to a different parent.").format(student), matches=[student]))
		else:
			errors.append(_enrollment_issue(row, "student", _("Student {0} was not found.").format(row.get("student"))))
	elif parent and not errors:
		conflicts = _trial_student_conflicts(parent, row)
		if conflicts:
			errors.extend(conflicts)
		else:
			matches = _find_student_matches(parent, {"student_name": row.get("student_name"), "student_dob": row.get("student_dob")})
			if len(matches) > 1:
				errors.append(_enrollment_issue(row, "student_name", _("Multiple Student records match this parent and student."), matches=matches))
			elif matches:
				student = matches[0]
				counts["students_reused"] += 1
			else:
				identity_matches = _find_student_identity_matches(row)
				if identity_matches:
					errors.append(_enrollment_issue(row, "student_name", _("Student {0} with DOB {1} already exists. Please resolve this duplicate manually.").format(row.get("student_name"), row.get("student_dob")), matches=identity_matches))
				counts["students_to_create"] += 1
				if _parent_student_count(parent):
					warnings.append(_enrollment_issue(row, "student_name", _("New student will be created under an existing parent. Check this is not a returning student.")))
	else:
		identity_matches = _find_student_identity_matches(row)
		if identity_matches:
			errors.append(_enrollment_issue(row, "student_name", _("Student {0} with DOB {1} already exists. Please resolve this duplicate manually.").format(row.get("student_name"), row.get("student_dob")), matches=identity_matches))
		counts["students_to_create"] += 1

	if student and _record_status("Student", student) not in ("", ACTIVE_STUDENT_STATUS):
		counts["students_to_update"] += 1
	if not row.get("student") and not row.get("student_dob"):
		warnings.append(_enrollment_issue(row, "student_dob", _("Student DOB is blank. Matching is less reliable.")))
	return {"parent": parent, "student": student, "counts": counts, "errors": errors, "warnings": warnings}


def _resolve_enrollment_schedule_preview(row):
	counts = defaultdict(int)
	errors = []
	context, reason = _resolve_enrollment_schedule(row)
	if reason or not context:
		errors.append(_enrollment_issue(row, "weekly_timeslot", reason or _("Weekly Timeslot could not be matched.")))
		return {"counts": counts, "errors": errors, "warnings": []}
	counts["weekly_timeslots_matched"] += 1
	if context.get("start_course_session"):
		counts["course_sessions_matched"] += 1
	return {**context, "counts": counts, "errors": errors, "warnings": []}


def _resolve_enrollment_schedule(row):
	if row.get("weekly_timeslot"):
		timeslot = _get_enrollment_import_timeslot(row.get("weekly_timeslot"))
		if not timeslot:
			return None, _("Weekly Timeslot {0} was not found.").format(row.get("weekly_timeslot"))
		if timeslot.get("status") and timeslot.get("status") != "Active":
			return None, _("Weekly Timeslot {0} is not Active.").format(row.get("weekly_timeslot"))
		term = row.get("term") or timeslot.get("term")
		if row.get("term") and timeslot.get("term") and row.get("term") != timeslot.get("term"):
			return None, _("Weekly Timeslot does not belong to term {0}.").format(row.get("term"))
		course = timeslot.get("course")
		if row.get("course"):
			resolved_course = _resolve_course(row.get("course"))
			if not resolved_course:
				return None, _("Course could not be matched from the submitted enrollment row.")
			if course and resolved_course != course:
				return None, _("Weekly Timeslot course does not match submitted course {0}.").format(row.get("course"))
			course = resolved_course
		return _enrollment_schedule_context(row, timeslot, term, course)

	term = row.get("term")
	if not term:
		return None, _("Term is required to match Weekly Timeslot.")
	if not frappe.db.exists("Term", term):
		return None, _("Term {0} was not found.").format(term)
	campus = _resolve_campus(row.get("campus"))
	if not campus:
		return None, _("Campus could not be matched from the submitted enrollment row.")
	course = _resolve_course(row.get("course"))
	if not course:
		return None, _("Course could not be matched from the submitted enrollment row.")
	parsed_session = _parse_class_session(row.get("session_label"))
	if not parsed_session:
		return None, _("Class session time could not be parsed from the submitted enrollment row.")
	timeslots = _get_enrollment_import_timeslots(
		term=term,
		campus=campus,
		course=course,
		day_of_week=parsed_session.get("day_of_week"),
		start_time=parsed_session.get("start_time"),
	)
	if not timeslots:
		return None, _("No Weekly Timeslot matched the submitted term, course, campus, weekday, and time.")
	if len(timeslots) > 1:
		return None, _("Multiple Weekly Timeslots matched the submitted term, course, campus, weekday, and time.")
	return _enrollment_schedule_context(row, timeslots[0], term, course)


def _enrollment_schedule_context(row, timeslot, term, course):
	if not term:
		return None, _("Term is required before creating an enrollment.")
	if not frappe.db.exists("Term", term):
		return None, _("Term {0} was not found.").format(term)
	start_course_session = row.get("start_course_session")
	if start_course_session:
		session = frappe.db.get_value(
			"Course Sessions",
			start_course_session,
			["name", "weekly_timeslot", "session_date", "status"],
			as_dict=True,
		)
		if not session:
			return None, _("Start Course Session {0} was not found.").format(start_course_session)
		if session.get("weekly_timeslot") != timeslot.get("name"):
			return None, _("Start Course Session does not belong to the matched Weekly Timeslot.")
		if session.get("status") == "Cancelled":
			return None, _("Start Course Session is cancelled.")
	return {
		"term": term,
		"course": course or timeslot.get("course"),
		"weekly_timeslot": timeslot.get("name"),
		"start_course_session": start_course_session,
	}, None


def _get_enrollment_import_timeslot(name):
	if not name or not frappe.db.exists("Weekly Timeslot", name):
		return None
	fields = ["name", "term", "course", "campus", "day_of_week", "start_time"]
	if _has_field("Weekly Timeslot", "status"):
		fields.append("status")
	return frappe.db.get_value(
		"Weekly Timeslot",
		name,
		fields,
		as_dict=True,
	)


def _get_enrollment_import_timeslots(term, campus, course, day_of_week, start_time):
	filters = {
		"term": term,
		"campus": campus,
		"course": course,
		"day_of_week": day_of_week,
		"start_time": start_time,
	}
	if _has_field("Weekly Timeslot", "status"):
		filters["status"] = "Active"
	fields = ["name", "term", "course", "campus", "day_of_week", "start_time"]
	if _has_field("Weekly Timeslot", "status"):
		fields.append("status")
	return frappe.get_all(
		"Weekly Timeslot",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit_page_length=0,
	)


def _run_enrollment_row(row):
	counts = defaultdict(int)
	warnings = []
	if row.get("duplicate_in_file"):
		counts["enrollment_duplicates_in_file"] += 1
		return _enrollment_preview_payload(row, counts, warnings=[_enrollment_issue(row, "enrollment", _("Duplicate row in this CSV was skipped."))], skipped=True)
	if row.get("enrollment"):
		if frappe.db.exists("Enrollment", row.get("enrollment")):
			counts["enrollments_existing_skipped"] += 1
			return _enrollment_preview_payload(row, counts, warnings=[_enrollment_issue(row, "enrollment", _("Existing Enrollment was skipped. Use the Enrollment workbench for updates."))], skipped=True)
		frappe.throw(_("Enrollment ID {0} was not found. Remove it for a new import row.").format(row.get("enrollment")))

	schedule = _resolve_enrollment_schedule_preview(row)
	if schedule.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in schedule.get("errors") or []))
	family_preview = _resolve_enrollment_family_preview(row)
	if family_preview.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in family_preview.get("errors") or []))
	warnings.extend(family_preview.get("warnings") or [])
	if row.get("submitted_status") and row.get("submitted_status") != "Planned":
		warnings.append(_enrollment_issue(row, "status", _("Enrollment import created a Planned record and ignored submitted status {0}.").format(row.get("submitted_status"))))

	family = _ensure_enrollment_family(row)
	_accumulate_counts(counts, family.get("counts") or {})
	warnings.extend(family.get("warnings") or [])
	parent = family.get("parent")
	student = family.get("student")
	term = schedule.get("term")
	weekly_timeslot = schedule.get("weekly_timeslot")
	if _enrollment_duplicate_exists(student, term, weekly_timeslot):
		counts["enrollments_duplicates_skipped"] += 1
		return _enrollment_preview_payload(row, counts, family=family, schedule=schedule, warnings=[_enrollment_issue(row, "enrollment", _("Matching Planned or Active Enrollment already exists and was skipped."))], skipped=True)

	doc = frappe.new_doc("Enrollment")
	doc.student = student
	doc.parent = parent
	doc.term = term
	doc.course = schedule.get("course")
	doc.weekly_timeslot = weekly_timeslot
	if schedule.get("start_course_session") and doc.meta.has_field("start_course_session"):
		doc.start_course_session = schedule.get("start_course_session")
	doc.enrollment_type = "Full-Term"
	doc.status = "Planned"
	if doc.meta.has_field("enrollment_date"):
		doc.enrollment_date = row.get("enrollment_date") or today()
	doc.insert(ignore_permissions=True)
	counts["enrollments_created"] += 1
	return {
		**_enrollment_preview_payload(row, counts, family=family, schedule=schedule, warnings=warnings),
		"enrollment": doc.name,
	}


def _ensure_enrollment_family(row):
	counts = defaultdict(int)
	warnings = []
	if row.get("parent"):
		parent = _ensure_existing_enrollment_parent(row, counts)
	else:
		user = _ensure_user(row.get("parent_email"), row.get("parent_name"))
		counts["users_created" if user.get("created") else "users_reused"] += 1
		parent_matches = _find_parent_matches(row.get("parent_email"))
		if len(parent_matches) > 1:
			frappe.throw(_("Multiple Parent records match email {0}.").format(row.get("parent_email")))
		customer = _find_customer_for_parent(row.get("parent_email"), parent_matches)
		parent_record = {
			"email": row.get("parent_email"),
			"parent_name": row.get("parent_name"),
			"parent_mobile": row.get("parent_mobile"),
			"parent_status": ACTIVE_PARENT_STATUS,
		}
		parent_result = _ensure_parent(parent_record, user.get("name"), customer)
		parent = parent_result.get("name")
		if parent_result.get("created"):
			counts["parents_created"] += 1
		elif parent_result.get("updated"):
			counts["parents_updated"] += 1
		else:
			counts["parents_reused"] += 1

	customer_before = _parent_customer(parent)
	customer_name = ensure_parent_customer(parent)
	if customer_name:
		counts["customers_reused" if customer_before == customer_name else "customers_created"] += 1

	if row.get("student"):
		student_result = _ensure_existing_enrollment_student(parent, row)
	else:
		student_result = _ensure_student(parent, {
			"row_number": row.get("row_number"),
			"student_name": row.get("student_name"),
			"student_dob": row.get("student_dob"),
			"student_status": ACTIVE_STUDENT_STATUS,
		})
	student = student_result.get("name") or student_result.get("student")
	if student_result.get("created"):
		counts["students_created"] += 1
	elif student_result.get("updated"):
		counts["students_updated"] += 1
	else:
		counts["students_reused"] += 1
	if not row.get("student_dob") and not row.get("student"):
		warnings.append(_enrollment_issue(row, "student_dob", _("Student was imported without DOB.")))
	return {"parent": parent, "student": student, "customer": customer_name, "counts": counts, "warnings": warnings}


def _ensure_existing_enrollment_parent(row, counts):
	doc = frappe.get_doc("Parent", row.get("parent"))
	changed = False
	if row.get("parent_email") and doc.meta.has_field("linked_user"):
		user = _ensure_user(row.get("parent_email"), row.get("parent_name") or doc.get("parent_name"))
		counts["users_created" if user.get("created") else "users_reused"] += 1
		if not doc.get("linked_user"):
			doc.linked_user = user.get("name")
			changed = True
	if row.get("parent_name") and not doc.get("parent_name"):
		doc.parent_name = row.get("parent_name")
		changed = True
	if row.get("parent_mobile") and doc.meta.has_field("mobile_number") and not doc.get("mobile_number"):
		doc.mobile_number = row.get("parent_mobile")
		changed = True
	if doc.meta.has_field("status") and doc.get("status") != ACTIVE_PARENT_STATUS:
		doc.status = ACTIVE_PARENT_STATUS
		changed = True
	if changed:
		doc.save(ignore_permissions=True)
		counts["parents_updated"] += 1
	else:
		counts["parents_reused"] += 1
	return doc.name


def _ensure_existing_enrollment_student(parent, row):
	doc = frappe.get_doc("Student", row.get("student"))
	parent_field = _student_parent_field()
	changed = False
	if parent_field:
		current_parent = doc.get(parent_field)
		if current_parent and current_parent != parent:
			frappe.throw(_("Student {0} belongs to a different parent.").format(doc.name))
		if not current_parent:
			doc.set(parent_field, parent)
			changed = True
	if row.get("student_name") and not doc.get("student_name"):
		doc.student_name = row.get("student_name")
		changed = True
	if row.get("student_dob") and doc.meta.has_field("date_of_birth") and not doc.get("date_of_birth"):
		doc.date_of_birth = row.get("student_dob")
		changed = True
	if doc.meta.has_field("status") and doc.get("status") != ACTIVE_STUDENT_STATUS:
		doc.status = ACTIVE_STUDENT_STATUS
		changed = True
	if changed:
		doc.save(ignore_permissions=True)
	return {"name": doc.name, "created": False, "updated": changed}


def _enrollment_duplicate_exists(student, term, weekly_timeslot):
	if not student or not term or not weekly_timeslot or not _doctype_available("Enrollment"):
		return False
	return bool(frappe.db.exists("Enrollment", {
		"student": student,
		"term": term,
		"weekly_timeslot": weekly_timeslot,
		"enrollment_type": "Full-Term",
		"status": ["in", ["Planned", "Active"]],
	}))


def _student_can_belong_to_parent(student, parent):
	parent_field = _student_parent_field()
	if not parent_field:
		return True
	current_parent = frappe.db.get_value("Student", student, parent_field)
	return not current_parent or current_parent == parent


def _parent_customer(parent):
	if parent and _has_field("Parent", "customer"):
		return frappe.db.get_value("Parent", parent, "customer")
	return None


def _parent_student_count(parent):
	parent_field = _student_parent_field()
	if not parent or not parent_field:
		return 0
	return len(frappe.get_all("Student", filters={parent_field: parent}, pluck="name", limit_page_length=0))


def _enrollment_preview_payload(row, counts, family=None, schedule=None, errors=None, warnings=None, skipped=False):
	family = family or {}
	schedule = schedule or {}
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent": family.get("parent") or row.get("parent"),
		"planned_parent_name": row.get("parent_name"),
		"student": family.get("student") or row.get("student"),
		"student_name": row.get("student_name") or family.get("student"),
		"student_dob": row.get("student_dob"),
		"student_count": 1,
		"term": schedule.get("term") or row.get("term"),
		"course": schedule.get("course") or row.get("course"),
		"weekly_timeslot": schedule.get("weekly_timeslot") or row.get("weekly_timeslot"),
		"session_label": row.get("session_label"),
		"start_course_session": schedule.get("start_course_session") or row.get("start_course_session"),
		"enrollment_type": "Full-Term",
		"status": "Planned",
		"skipped": skipped,
		"counts": dict(counts),
		"errors": errors or [],
		"warnings": warnings or [],
	}


def _enrollment_issue(row, field, message, matches=None):
	issue = {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"field": field,
		"message": str(message),
	}
	if matches:
		issue["matches"] = matches
	return issue


def _enrollment_id_from_row(row):
	explicit = _normalize_spaces(_first(row, ["enrollment", "Enrollment", "Enrollment ID", "name", "Name (Enrollment)"]))
	if explicit:
		return explicit
	generic_id = _normalize_spaces(_first(row, ["ID", "id"]))
	if generic_id and _looks_like_frappe_enrollment_row(row):
		return generic_id
	return ""


def _looks_like_frappe_enrollment_row(row):
	frappe_enrollment_keys = ["Weekly Timeslot", "Enrollment Type", "Enrollment Date", "Start Course Session"]
	if any(row.get(key) for key in frappe_enrollment_keys):
		return True
	return all(key in row for key in ["Student", "Parent", "Term"])


def _record_status(doctype, name):
	if name and _has_field(doctype, "status"):
		return frappe.db.get_value(doctype, name, "status") or ""
	return ""


def _enrollment_parent_key(row):
	return row.get("parent") or row.get("parent_email") or row.get("parent_name")


def _enrollment_import_key(row):
	return "|".join(
		_normalized_key(part)
		for part in [
			row.get("enrollment"),
			row.get("parent") or row.get("parent_email"),
			row.get("student") or row.get("student_name"),
			row.get("student_dob"),
			row.get("term"),
			row.get("weekly_timeslot") or row.get("session_label"),
		]
	)


def _build_trial_inquiry_import_batch(payload=None):
	payload = _get_payload(payload)
	rows = payload.get("rows") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		frappe.throw(_("Import rows must be a list."))

	row_results = []
	trials = []
	seen_keys = set()
	for index, raw_row in enumerate(rows, start=1):
		if not isinstance(raw_row, dict):
			row_results.append(_row_error(index, None, "row", _("Row must be an object.")))
			continue
		row = _normalize_trial_inquiry_row(raw_row, index)
		key = _trial_import_key(row)
		if key and key in seen_keys:
			row["duplicate_in_file"] = True
		elif key:
			seen_keys.add(key)
		row_results.append(row)
		if not row.get("errors"):
			trials.append(row)
	return {"rows": row_results, "trials": trials}


def _normalize_trial_inquiry_row(raw_row, row_number):
	row = {str(key or "").strip(): _clean_text(value) for key, value in raw_row.items()}
	parent_email = _normalize_email(_first(row, ["parent_email", "Parent Email", "Contact Email Address", "email", "Email"]))
	parent_name = _normalize_spaces(_first(row, ["parent_name", "Parent Name", "contact_name", "Contact Name"]))
	parent_phone = _normalize_phone(_first(row, ["parent_phone", "parent_mobile", "Phone bumber", "Phone Number", "phone", "Phone"]))
	student_name = _normalize_spaces(_first(row, ["student_name", "Student Name", "submitted_student_name"]))
	student_dob = _parse_date(_first(row, ["student_dob", "Student Birthday", "Date of Birth", "dob", "DOB"]))
	campus = _normalize_spaces(_first(row, ["campus", "Which Campus?", "Campus"]))
	class_type = _normalize_spaces(_first(row, ["class_type", "Class Type", "course", "Course"]))
	form_name = _normalize_spaces(_first(row, ["form_name", "Form Name"]))
	session_label = _normalize_spaces(_first(row, ["session_label", "Which session?", "submitted_class_session", "Class Session"]))
	trial_class_date = _parse_date(_first(row, ["trial_class_date", "Trial Class Date", "submitted_trial_date"]))
	trial_request_date = _parse_datetime_string(_first(row, ["trial_request_date", "Trial Request date", "created_at", "Creation"]))
	referral_source = _normalize_spaces(_first(row, ["referral_source", "How do you know us?", "Referal", "Referral"]))
	referral_detail = _normalize_spaces(_first(row, ["referral_detail", "notes", "Notes", "Unnamed: 16"]))
	source_id = _normalize_spaces(_first(row, ["source_id", "ID", "id"]))

	normalized = {
		"row_number": row_number,
		"source": row,
		"source_id": source_id,
		"parent_email": parent_email,
		"parent_name": parent_name,
		"parent_phone": parent_phone,
		"student_name": student_name,
		"student_dob": student_dob,
		"campus": campus,
		"class_type": class_type,
		"form_name": form_name or _normalize_spaces(" ".join(part for part in [campus, class_type] if part)),
		"session_label": session_label,
		"trial_class_date": trial_class_date,
		"trial_request_date": trial_request_date,
		"referral_source": referral_source,
		"referral_detail": referral_detail,
		"errors": [],
	}
	if not parent_email:
		normalized["errors"].append(_field_error("parent_email", _("Parent email is required.")))
	if not parent_name:
		normalized["errors"].append(_field_error("parent_name", _("Parent name is required.")))
	if not student_name:
		normalized["errors"].append(_field_error("student_name", _("Student name is required.")))
	if not trial_class_date:
		normalized["errors"].append(_field_error("trial_class_date", _("Trial class date is required.")))
	if not session_label:
		normalized["errors"].append(_field_error("session_label", _("Trial session label is required.")))
	if not normalized["form_name"]:
		normalized["errors"].append(_field_error("form_name", _("Campus and class type, or form name, is required.")))
	return normalized


def _preview_trial_inquiry_batch(batch):
	result = _empty_result(dry_run=True)
	result["input"] = {
		"row_count": len(batch.get("rows") or []),
		"parent_count": len({row.get("parent_email") for row in batch.get("trials") or [] if row.get("parent_email")}),
		"student_count": len(batch.get("trials") or []),
		"trial_count": len(batch.get("trials") or []),
	}

	for row in batch.get("rows") or []:
		for error in row.get("errors") or []:
			result["errors"].append({"row": row.get("row_number"), "parent_email": row.get("parent_email"), **error})

	for row in batch.get("trials") or []:
		preview = _preview_trial_inquiry_row(row)
		result["parents"].append(preview)
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])

	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_trial_inquiry_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	if row.get("duplicate_in_file"):
		counts["trial_duplicates_in_file"] += 1
		warnings.append(_trial_issue(row, "source_id", _("Duplicate row in this CSV. The later duplicate will be skipped.")))
		return _trial_preview_payload(row, counts, warnings=warnings, skipped=True)

	family = _resolve_trial_family_preview(row)
	_accumulate_counts(counts, family.get("counts") or {})
	errors.extend(family.get("errors") or [])
	warnings.extend(family.get("warnings") or [])

	session = _resolve_trial_session_preview(row)
	_accumulate_counts(counts, session.get("counts") or {})
	errors.extend(session.get("errors") or [])
	warnings.extend(session.get("warnings") or [])

	parent = family.get("parent")
	student = family.get("student")
	course_session = session.get("course_session")
	if not errors and course_session:
		if parent and student and _trial_inquiry_duplicate_exists(parent, student, course_session):
			counts["trial_inquiries_duplicates_skipped"] += 1
			warnings.append(_trial_issue(row, "trial_inquiry", _("Matching Trial Lesson Inquiry already exists and will be skipped.")))
		elif student and _attendance_student_session_conflict(student, course_session):
			counts["attendance_duplicates_skipped"] += 1
			warnings.append(_trial_issue(row, "attendance", _("This student is already listed for this session and will be skipped.")))
		else:
			counts["trial_inquiries_to_create"] += 1
			counts["trial_attendance_to_create"] += 1
	return _trial_preview_payload(row, counts, family=family, session=session, errors=errors, warnings=warnings)


def _trial_preview_payload(row, counts, family=None, session=None, errors=None, warnings=None, skipped=False):
	family = family or {}
	session = session or {}
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent": family.get("parent"),
		"planned_parent_name": row.get("parent_name"),
		"student": family.get("student"),
		"student_name": row.get("student_name"),
		"student_dob": row.get("student_dob"),
		"student_count": 1,
		"campus": row.get("campus"),
		"class_type": row.get("class_type"),
		"session_label": row.get("session_label"),
		"trial_class_date": row.get("trial_class_date"),
		"course_session": session.get("course_session"),
		"skipped": skipped,
		"counts": dict(counts),
		"errors": errors or [],
		"warnings": warnings or [],
	}


def _resolve_trial_family_preview(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	parent_matches = _find_parent_matches(row.get("parent_email"))
	if len(parent_matches) > 1:
		errors.append(_trial_issue(row, "parent_email", _("Multiple Parent records match email {0}.").format(row.get("parent_email")), matches=parent_matches))
		return {"counts": counts, "errors": errors, "warnings": warnings}

	parent = parent_matches[0] if parent_matches else None
	if parent:
		counts["parents_reused"] += 1
	else:
		counts["parents_to_create"] += 1

	student = None
	if parent:
		conflicts = _trial_student_conflicts(parent, row)
		if conflicts:
			errors.extend(conflicts)
		else:
			matches = _find_student_matches(parent, {"student_name": row.get("student_name"), "student_dob": row.get("student_dob")})
			if len(matches) > 1:
				errors.append(_trial_issue(row, "student_name", _("Multiple Student records match this parent and student."), matches=matches))
			elif matches:
				student = matches[0]
				counts["students_reused"] += 1
				if not row.get("student_dob"):
					warnings.append(_trial_issue(row, "student_dob", _("Student was matched by name only because DOB is blank.")))
			else:
				counts["students_to_create"] += 1
	else:
		counts["students_to_create"] += 1
		if not row.get("student_dob"):
			warnings.append(_trial_issue(row, "student_dob", _("New student will be created without DOB.")))
	return {"parent": parent, "student": student, "counts": counts, "errors": errors, "warnings": warnings}


def _resolve_trial_session_preview(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	session_context, review_reason = _resolve_trial_import_session(row)
	if review_reason or not session_context:
		errors.append(_trial_issue(row, "course_session", review_reason or _("Course Session could not be matched.")))
		return {"counts": counts, "errors": errors, "warnings": warnings}
	counts["course_sessions_matched"] += 1
	return {
		"course_session": session_context.get("course_session"),
		"course": session_context.get("course"),
		"counts": counts,
		"errors": errors,
		"warnings": warnings,
	}


def _resolve_trial_import_session(row):
	campus = _resolve_campus(row.get("campus"))
	if not campus:
		return None, _("Campus could not be matched from the submitted trial import row.")

	parsed_session = _parse_class_session(row.get("session_label"))
	if not parsed_session:
		return None, _("Class session time could not be parsed from the submitted trial import row.")

	trial_date = row.get("trial_class_date")
	if not trial_date:
		return None, _("Trial date was not submitted.")

	course = _resolve_trial_import_course(row)
	if not course:
		return None, _("Course could not be matched from the submitted trial import row.")

	timeslots = _get_trial_import_timeslots(
		campus=campus,
		day_of_week=parsed_session.get("day_of_week"),
		start_time=parsed_session.get("start_time"),
		course=course,
	)
	if not timeslots:
		return None, _("No Weekly Timeslot matched the submitted course, campus, weekday, and time.")

	course_session = _get_trial_import_course_session(timeslots, trial_date)
	if course_session.get("reason"):
		return None, course_session.get("reason")
	timeslot = course_session.get("timeslot") or {}
	return {
		"course_session": course_session.get("name"),
		"course": timeslot.get("course"),
		"timeslot": timeslot.get("name"),
		"campus": campus,
	}, None


def _resolve_trial_import_course(row):
	for value in (row.get("class_type"), row.get("form_name")):
		course = _resolve_course(value)
		if course:
			return course
	return None


def _get_trial_import_timeslots(campus, day_of_week, start_time, course=None):
	filters = {
		"campus": campus,
		"day_of_week": day_of_week,
		"start_time": start_time,
	}
	if course:
		filters["course"] = course
	if _has_field("Weekly Timeslot", "status"):
		filters["status"] = "Active"
	fields = ["name", "course", "campus", "start_time", "day_of_week"]
	return frappe.get_all(
		"Weekly Timeslot",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit_page_length=0,
	)


def _get_trial_import_course_session(timeslots, trial_date):
	timeslots_by_name = {timeslot.get("name"): timeslot for timeslot in timeslots if timeslot.get("name")}
	sessions = frappe.get_all(
		"Course Sessions",
		filters={"weekly_timeslot": ["in", list(timeslots_by_name)], "session_date": trial_date},
		fields=["name", "weekly_timeslot", "session_date", "status"],
		order_by="modified desc",
		limit_page_length=0,
	)
	if not sessions:
		return {"reason": _("No Course Session exists for the matched Weekly Timeslot and trial date.")}
	if len(sessions) > 1:
		return {"reason": _("Multiple Course Sessions matched the submitted course, campus, weekday, time, and trial date.")}
	return {"name": sessions[0].get("name"), "timeslot": timeslots_by_name.get(sessions[0].get("weekly_timeslot"))}


def _run_trial_inquiry_row(row):
	counts = defaultdict(int)
	warnings = []
	if row.get("duplicate_in_file"):
		counts["trial_duplicates_in_file"] += 1
		return _trial_preview_payload(row, counts, warnings=[_trial_issue(row, "source_id", _("Duplicate row in this CSV was skipped."))], skipped=True)

	session = _resolve_trial_session_preview(row)
	if session.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in session.get("errors") or []))

	family = _ensure_trial_family(row)
	_accumulate_counts(counts, family.get("counts") or {})
	warnings.extend(family.get("warnings") or [])

	parent = family.get("parent")
	student = family.get("student")
	course_session = session.get("course_session")
	if _trial_inquiry_duplicate_exists(parent, student, course_session):
		counts["trial_inquiries_duplicates_skipped"] += 1
		return _trial_preview_payload(row, counts, family=family, session=session, warnings=[_trial_issue(row, "trial_inquiry", _("Matching Trial Lesson Inquiry already exists and was skipped."))], skipped=True)
	if _attendance_student_session_conflict(student, course_session):
		counts["attendance_duplicates_skipped"] += 1
		return _trial_preview_payload(row, counts, family=family, session=session, warnings=[_trial_issue(row, "attendance", _("This student is already listed for this session and was skipped."))], skipped=True)

	payload = _trial_inquiry_payload(row)
	payload.update({"parent": parent, "student": student, "course_session": course_session})
	detail = create_inquiry_core(payload, source=payload.get("source") or "Trial Import", actor=frappe.session.user)
	counts["trial_inquiries_created"] += 1
	counts["trial_attendance_created"] += 1
	return {
		**_trial_preview_payload(row, counts, family=family, session=session, warnings=warnings),
		"inquiry": (detail.get("inquiry") or {}).get("id"),
	}


def _ensure_trial_family(row):
	counts = defaultdict(int)
	warnings = []
	email = row.get("parent_email")
	user = _ensure_trial_user(email, row.get("parent_name"))
	counts["users_created" if user.get("created") else "users_reused"] += 1
	parent = _ensure_trial_parent(row, user.get("name"))
	counts["parents_created" if parent.get("created") else "parents_reused"] += 1
	student = _ensure_trial_student(parent.get("name"), row)
	counts["students_created" if student.get("created") else "students_reused"] += 1
	if student.get("matched_by_name_only"):
		warnings.append(_trial_issue(row, "student_dob", _("Existing student reused by name only because DOB is blank.")))
	return {"parent": parent.get("name"), "student": student.get("name"), "counts": counts, "warnings": warnings}


def _ensure_trial_user(email, parent_name):
	user_name = _find_user(email)
	if user_name:
		return {"name": user_name, "created": False}
	user_doc = frappe.new_doc("User")
	user_doc.email = email
	user_doc.first_name = parent_name or email
	user_doc.enabled = 1
	user_doc.user_type = "Website User"
	user_doc.send_welcome_email = 0
	user_doc.flags.ignore_permissions = True
	user_doc.insert(ignore_permissions=True)
	return {"name": user_doc.name, "created": True}


def _ensure_trial_parent(row, user):
	matches = _find_parent_matches(row.get("parent_email"))
	if len(matches) > 1:
		frappe.throw(_("Multiple Parent records match email {0}.").format(row.get("parent_email")))
	if matches:
		doc = frappe.get_doc("Parent", matches[0])
		created = False
	else:
		doc = frappe.new_doc("Parent")
		created = True

	changed = False
	if not doc.get("parent_name"):
		doc.parent_name = row.get("parent_name") or row.get("parent_email")
		changed = True
	if doc.meta.has_field("linked_user") and not doc.get("linked_user"):
		doc.linked_user = user
		changed = True
	if row.get("parent_phone") and doc.meta.has_field("mobile_number") and not doc.get("mobile_number"):
		doc.mobile_number = row.get("parent_phone")
		changed = True
	for fieldname in ("email", "email_id", "contact_email"):
		if row.get("parent_email") and doc.meta.has_field(fieldname) and not doc.get(fieldname):
			doc.set(fieldname, row.get("parent_email"))
			changed = True
	if created and doc.meta.has_field("status") and not doc.get("status"):
		doc.status = ACTIVE_PARENT_STATUS
	if created:
		doc.insert(ignore_permissions=True)
	elif changed:
		doc.save(ignore_permissions=True)
	return {"name": doc.name, "created": created, "updated": changed and not created}


def _ensure_trial_student(parent, row):
	conflicts = _trial_student_conflicts(parent, row)
	if conflicts:
		frappe.throw("; ".join(error.get("message") for error in conflicts))
	matches = _find_student_matches(parent, {"student_name": row.get("student_name"), "student_dob": row.get("student_dob")})
	if len(matches) > 1:
		frappe.throw(_("Multiple Student records match row {0}.").format(row.get("row_number")))
	if matches:
		return {"name": matches[0], "created": False, "matched_by_name_only": not row.get("student_dob")}

	doc = frappe.new_doc("Student")
	doc.student_name = row.get("student_name")
	parent_field = _student_parent_field()
	if parent_field:
		doc.set(parent_field, parent)
	if row.get("student_dob") and doc.meta.has_field("date_of_birth"):
		doc.date_of_birth = row.get("student_dob")
	elif not row.get("student_dob"):
		doc.name = _make_trial_no_dob_student_docname(row.get("student_name"))
		doc.flags.name_set = True
	if doc.meta.has_field("status"):
		doc.status = INACTIVE_STUDENT_STATUS
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "created": True}


def _trial_student_conflicts(parent, row):
	if not parent or not _doctype_available("Student"):
		return []
	parent_field = _student_parent_field()
	if not parent_field:
		return []
	target_name = _normalized_key(row.get("student_name"))
	target_dob = row.get("student_dob")
	students = frappe.get_all(
		"Student",
		filters={parent_field: parent},
		fields=["name", "student_name", "date_of_birth"],
		limit_page_length=0,
	)
	errors = []
	for student in students:
		same_name = _normalized_key(student.get("student_name")) == target_name
		same_dob = target_dob and str(student.get("date_of_birth") or "") == target_dob
		if same_name and target_dob and student.get("date_of_birth") and str(student.get("date_of_birth")) != target_dob:
			errors.append(_trial_issue(row, "student_dob", _("Existing student {0} has the same name but different DOB.").format(student.name), matches=[student.name]))
		if same_dob and not same_name:
			errors.append(_trial_issue(row, "student_dob", _("Existing student {0} has the same DOB but different name.").format(student.name), matches=[student.name]))
	return errors


def _trial_inquiry_payload(row):
	return {
		"inquiry_type": "Trial Lesson",
		"source": "Trial Import",
		"parent_name": row.get("parent_name"),
		"contact_name": row.get("parent_name"),
		"phone": row.get("parent_phone"),
		"contact_phone": row.get("parent_phone"),
		"email": row.get("parent_email"),
		"contact_email": row.get("parent_email"),
		"student_name": row.get("student_name"),
		"submitted_student_name": row.get("student_name"),
		"date_of_birth": row.get("student_dob"),
		"submitted_student_dob": row.get("student_dob"),
		"submitted_form_name": row.get("form_name"),
		"submitted_class_session": row.get("session_label"),
		"submitted_trial_date": row.get("trial_class_date"),
		"appointment_date": row.get("trial_class_date"),
		"campus": row.get("campus"),
		"preferred_course": row.get("class_type"),
		"referral_source": row.get("referral_source"),
		"referral_detail": row.get("referral_detail") or (f"Source ID: {row.get('source_id')}" if row.get("source_id") else ""),
	}


def _trial_import_key(row):
	return "|".join(
		_normalized_key(part)
		for part in [
			row.get("parent_email"),
			row.get("student_name"),
			row.get("student_dob"),
			row.get("trial_class_date"),
			row.get("session_label"),
		]
	)


def _trial_inquiry_duplicate_exists(parent, student, course_session):
	return bool(frappe.db.exists("Inquiry", {
		"inquiry_type": "Trial Lesson",
		"parent": parent,
		"student": student,
		"course_session": course_session,
		"status": ["!=", "Cancelled"],
	}))


def _attendance_student_session_conflict(student, course_session):
	if not _doctype_available(ATTENDANCE_DOCTYPE):
		return False
	return bool(frappe.db.exists(ATTENDANCE_DOCTYPE, {
		"student": student,
		"course_session": course_session,
		"status": ["!=", "Cancelled"],
	}))


def _trial_issue(row, field, message, matches=None):
	issue = {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"field": field,
		"message": str(message),
	}
	if matches:
		issue["matches"] = matches
	return issue


def _parse_datetime_string(value):
	value = (value or "").strip()
	if not value:
		return ""
	for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
		try:
			return datetime.strptime(value, fmt).isoformat(sep=" ")
		except Exception:
			pass
	return _parse_date(value)


def _make_trial_no_dob_student_docname(student_name):
	base = (student_name or "Student").strip()
	for _ in range(5):
		name = f"{base}-trial-no-dob-{frappe.generate_hash(length=8)}"
		if not frappe.db.exists("Student", name):
			return name
	return f"{base}-trial-no-dob-{frappe.generate_hash(length=12)}"


def _build_store_credit_import_batch(payload=None):
	payload = _get_payload(payload)
	rows = payload.get("rows") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		frappe.throw(_("Import rows must be a list."))

	row_results = []
	credits = []
	for index, raw_row in enumerate(rows, start=1):
		if not isinstance(raw_row, dict):
			row_results.append(_row_error(index, None, "row", _("Row must be an object.")))
			continue
		row = _normalize_store_credit_row(raw_row, index)
		row_results.append(row)
		if not row.get("errors"):
			credits.append(row)
	return {"rows": row_results, "credits": credits}


def _normalize_store_credit_row(raw_row, row_number):
	row = {str(key or "").strip(): _clean_text(value) for key, value in raw_row.items()}
	parent_email = _normalize_email(_first(row, ["parent_email", "Parent Email", "email", "Email", "linked_user", "Linked User"]))
	parent = _normalize_spaces(_first(row, ["parent", "Parent", "parent_id", "Parent ID"]))
	customer = _normalize_spaces(_first(row, ["customer", "Customer", "customer_name", "Customer Name"]))
	amount = _money(_first(row, ["amount", "Amount", "store_credit", "Store Credit", "recommended_store_credit_amount", "Recommended Store Credit Amount"]))
	reason = _normalize_spaces(_first(row, ["reason", "Reason"])) or "Legacy store credit opening balance"
	notes = _normalize_spaces(_first(row, ["notes", "Notes"]))

	normalized = {
		"row_number": row_number,
		"source": row,
		"parent_email": parent_email,
		"parent": parent,
		"customer": customer,
		"amount": amount,
		"reason": reason,
		"notes": notes,
		"errors": [],
	}
	if not parent_email and not parent and not customer:
		normalized["errors"].append(_field_error("parent_email", _("Parent email, parent, or customer is required.")))
	if amount <= 0:
		normalized["errors"].append(_field_error("amount", _("Store credit amount must be greater than zero.")))
	return normalized


def _preview_store_credit_batch(batch):
	result = _empty_result(dry_run=True)
	result["input"] = {
		"row_count": len(batch.get("rows") or []),
		"parent_count": len(batch.get("credits") or []),
		"student_count": 0,
		"store_credit_amount": sum(flt(row.get("amount")) for row in batch.get("credits") or []),
	}

	for row in batch.get("rows") or []:
		for error in row.get("errors") or []:
			result["errors"].append({"row": row.get("row_number"), "parent_email": row.get("parent_email"), **error})

	for row in batch.get("credits") or []:
		preview = _preview_store_credit_row(row)
		result["parents"].append(preview)
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])

	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_store_credit_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	resolved = _resolve_store_credit_target(row)
	if resolved.get("errors"):
		errors.extend(resolved.get("errors"))
	else:
		row.update({"parent": resolved.get("parent"), "customer": resolved.get("customer")})
		if _store_credit_duplicate_exists(row):
			counts["store_credit_duplicates_skipped"] += 1
			warnings.append({
				"row": row.get("row_number"),
				"parent_email": row.get("parent_email"),
				"field": "amount",
				"message": _("Matching store credit ledger entry already exists and will be skipped."),
			})
		else:
			counts["store_credits_to_create"] += 1
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent": row.get("parent") or resolved.get("parent"),
		"customer": row.get("customer") or resolved.get("customer"),
		"amount": row.get("amount"),
		"reason": row.get("reason"),
		"notes": row.get("notes"),
		"counts": dict(counts),
		"errors": errors,
		"warnings": warnings,
	}


def _run_store_credit_row(row):
	resolved = _resolve_store_credit_target(row)
	if resolved.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in resolved.get("errors") or []))
	row = {**row, "parent": resolved.get("parent"), "customer": resolved.get("customer")}
	counts = defaultdict(int)
	if _store_credit_duplicate_exists(row):
		counts["store_credit_duplicates_skipped"] += 1
		return {
			"row": row.get("row_number"),
			"parent_email": row.get("parent_email"),
			"parent": row.get("parent"),
			"customer": row.get("customer"),
			"amount": row.get("amount"),
			"skipped": True,
			"counts": dict(counts),
		}

	entry = adjust_store_credit(
		parent=row.get("parent"),
		customer=row.get("customer"),
		amount=row.get("amount"),
		reason=row.get("reason"),
		notes=row.get("notes"),
	)
	counts["store_credits_created"] += 1
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent": entry.parent,
		"customer": entry.customer,
		"amount": row.get("amount"),
		"store_credit_ledger": entry.name,
		"counts": dict(counts),
	}


def _resolve_store_credit_target(row):
	parent = row.get("parent")
	customer = row.get("customer")
	email = row.get("parent_email")
	errors = []

	if parent and not frappe.db.exists("Parent", parent):
		errors.append({"row": row.get("row_number"), "parent_email": email, "field": "parent", "message": _("Parent {0} was not found.").format(parent)})
		parent = None
	if customer and _doctype_available("Customer") and not frappe.db.exists("Customer", customer):
		errors.append({"row": row.get("row_number"), "parent_email": email, "field": "customer", "message": _("Customer {0} was not found.").format(customer)})
		customer = None

	if not parent and email:
		matches = _find_parent_matches(email)
		if len(matches) > 1:
			errors.append({
				"row": row.get("row_number"),
				"parent_email": email,
				"field": "parent_email",
				"message": _("Multiple Parent records match email {0}.").format(email),
				"matches": matches,
			})
		elif matches:
			parent = matches[0]

	if parent and not customer and _has_field("Parent", "customer"):
		customer = frappe.db.get_value("Parent", parent, "customer")
	if not customer and email:
		customer = _find_customer_by_email(email)

	if not parent and not customer:
		errors.append({
			"row": row.get("row_number"),
			"parent_email": email,
			"field": "parent_email",
			"message": _("Could not match this row to a Parent or Customer."),
		})
	if not customer:
		errors.append({
			"row": row.get("row_number"),
			"parent_email": email,
			"field": "customer",
			"message": _("Customer is required for store credit."),
		})
	return {"parent": parent, "customer": customer, "errors": errors}


def _store_credit_duplicate_exists(row):
	if not _doctype_available(LEDGER_DOCTYPE):
		return False
	filters = {
		"customer": row.get("customer"),
		"transaction_type": "Manual Adjustment",
		"credit_amount": flt(row.get("amount")),
		"debit_amount": 0,
		"reason": row.get("reason"),
	}
	if row.get("notes"):
		filters["notes"] = row.get("notes")
	if row.get("parent") and frappe.db.has_column(LEDGER_DOCTYPE, "parent"):
		filters["parent"] = row.get("parent")
	return bool(frappe.db.exists(LEDGER_DOCTYPE, filters))


def _build_import_batch(payload=None):
	payload = _get_payload(payload)
	rows = payload.get("rows") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		frappe.throw(_("Import rows must be a list."))
	default_student_status = _normalize_student_status(payload.get("default_student_status")) if isinstance(payload, dict) else ACTIVE_STUDENT_STATUS
	default_parent_status = _normalize_parent_status(payload.get("default_parent_status")) if isinstance(payload, dict) else ACTIVE_PARENT_STATUS

	parents_by_email = {}
	row_results = []
	for index, raw_row in enumerate(rows, start=1):
		if not isinstance(raw_row, dict):
			row_results.append(_row_error(index, None, "row", _("Row must be an object.")))
			continue
		row = _normalize_raw_row(raw_row, index, default_student_status=default_student_status, default_parent_status=default_parent_status)
		row_results.append(row)
		if row.get("errors"):
			continue
		email = row.get("parent_email")
		parent = parents_by_email.setdefault(
			email,
			{
				"email": email,
				"parent_name": "",
				"parent_mobile": "",
				"parent_status": "",
				"customer_name": "",
				"customer_group": "",
				"territory": "",
				"legacy_balance": 0,
				"students": [],
				"row_numbers": [],
			},
		)
		parent["row_numbers"].append(index)
		_merge_parent_fields(parent, row)
		if row.get("student_name"):
			parent["students"].append(row)

	return {
		"rows": row_results,
		"parents": list(parents_by_email.values()),
	}


def _normalize_raw_row(raw_row, row_number, default_student_status=ACTIVE_STUDENT_STATUS, default_parent_status=ACTIVE_PARENT_STATUS):
	row = {str(key or "").strip(): _clean_text(value) for key, value in raw_row.items()}
	is_student_row = bool(_first(row, ["student_name", "student", "Student Name", "ID", "Date of Birth", "date_of_birth", "student_dob"]))
	is_customer_row = bool(_first(row, ["Students", "Balance", "Phone number"])) and not is_student_row

	parent_email = _normalize_email(_first(row, ["parent_email", "Parent Email", "Email", "email", "linked_user"]))
	parent_name = _first(row, ["parent_name", "Parent Name", "customer_name", "Customer Name"])
	student_name = _first(row, ["student_name", "Student Name", "student"])

	if not parent_name and is_customer_row:
		parent_name = _first(row, ["Name"])
	if not student_name and is_student_row:
		student_name = _first(row, ["Name"])

	normalized = {
		"row_number": row_number,
		"source": row,
		"parent_email": parent_email,
		"parent_name": _normalize_spaces(parent_name),
		"parent_mobile": _normalize_phone(_first(row, ["parent_mobile", "Parent Mobile", "Phone number", "phone", "mobile_number"])),
		"parent_status": _normalize_parent_status(_first(row, ["parent_status", "Parent Status", "Parent status", "customer_status", "Customer Status"]) or default_parent_status),
		"customer_name": _normalize_spaces(_first(row, ["customer_name", "Customer Name"])),
		"customer_group": _normalize_spaces(_first(row, ["customer_group", "Customer Group"])),
		"territory": _normalize_spaces(_first(row, ["territory", "Territory"])),
		"legacy_balance": _money(_first(row, ["legacy_balance", "Balance", "balance"])),
		"student_name": _normalize_spaces(student_name),
		"student_dob": _parse_date(_first(row, ["student_dob", "Date of Birth", "date_of_birth", "dob"])),
		"student_status": _normalize_student_status(_first(row, ["student_status", "Student Status", "status"]) or default_student_status),
		"external_student_id": _normalize_spaces(_first(row, ["student_external_id", "external_student_id", "ID"])),
		"errors": [],
	}
	if not normalized["parent_email"]:
		normalized["errors"].append(_field_error("parent_email", _("Parent email is required.")))
	if not normalized["parent_name"]:
		normalized["errors"].append(_field_error("parent_name", _("Parent name is required.")))
	if is_student_row and not normalized["student_name"]:
		normalized["errors"].append(_field_error("student_name", _("Student name is required.")))
	return normalized


def _merge_parent_fields(parent, row):
	for target, source in (
		("parent_name", "parent_name"),
		("parent_mobile", "parent_mobile"),
		("parent_status", "parent_status"),
		("customer_name", "customer_name"),
		("customer_group", "customer_group"),
		("territory", "territory"),
	):
		if row.get(source) and not parent.get(target):
			parent[target] = row.get(source)
	if row.get("legacy_balance"):
		parent["legacy_balance"] = flt(parent.get("legacy_balance")) + flt(row.get("legacy_balance"))


def _preview_batch(batch):
	result = _empty_result(dry_run=True)
	result["input"] = {
		"row_count": len(batch.get("rows") or []),
		"parent_count": len(batch.get("parents") or []),
		"student_count": sum(len(parent.get("students") or []) for parent in batch.get("parents") or []),
	}

	for row in batch.get("rows") or []:
		for error in row.get("errors") or []:
			result["errors"].append({"row": row.get("row_number"), **error})

	for parent_record in batch.get("parents", []):
		parent_preview = _preview_parent_record(parent_record)
		result["parents"].append(parent_preview)
		_accumulate_counts(result["counts"], parent_preview.get("counts") or {})
		result["errors"].extend(parent_preview.get("errors") or [])
		result["warnings"].extend(parent_preview.get("warnings") or [])

	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_parent_record(parent_record):
	email = parent_record.get("email")
	parent_matches = _find_parent_matches(email)
	customer = _find_customer_for_parent(email, parent_matches)
	user = _find_user(email)
	errors = []
	warnings = []
	counts = defaultdict(int)

	if len(parent_matches) > 1:
		errors.append({
			"row": parent_record.get("row_numbers", [None])[0],
			"field": "parent_email",
			"message": _("Multiple Parent records match email {0}.").format(email),
			"matches": parent_matches,
		})

	parent_name = parent_matches[0] if len(parent_matches) == 1 else None
	if parent_name:
		counts["parents_reused"] += 1
	else:
		counts["parents_to_create"] += 1
	if customer:
		counts["customers_reused"] += 1
	else:
		counts["customers_to_create"] += 1
	if user:
		counts["users_reused"] += 1
	else:
		counts["users_to_create"] += 1
	if flt(parent_record.get("legacy_balance")):
		counts["legacy_balances_detected"] += 1
		warnings.append({
			"row": parent_record.get("row_numbers", [None])[0],
			"field": "Balance",
			"message": _("Legacy balance {0} was detected but will not be imported by the parent/student import.").format(parent_record.get("legacy_balance")),
			"parent_email": email,
		})

	student_previews = []
	for student_row in parent_record.get("students") or []:
		student_preview = _preview_student_row(parent_name, student_row)
		student_previews.append(student_preview)
		_accumulate_counts(counts, student_preview.get("counts") or {})
		errors.extend(student_preview.get("errors") or [])

	return {
		"parent_email": email,
		"parent_name": parent_name,
		"planned_parent_name": parent_record.get("parent_name"),
		"planned_parent_status": parent_record.get("parent_status"),
		"user": user,
		"customer": customer,
		"student_count": len(parent_record.get("students") or []),
		"students": student_previews,
		"counts": dict(counts),
		"errors": errors,
		"warnings": warnings,
	}


def _preview_student_row(parent_name, student_row):
	counts = defaultdict(int)
	errors = []
	matches = _find_student_matches(parent_name, student_row) if parent_name else []
	if len(matches) > 1:
		errors.append({
			"row": student_row.get("row_number"),
			"field": "student_name",
			"message": _("Multiple Student records match this row."),
			"matches": matches,
		})
	elif matches:
		counts["students_reused"] += 1
	else:
		identity_matches = _find_student_identity_matches(student_row)
		if identity_matches:
			errors.append({
				"row": student_row.get("row_number"),
				"field": "student_name",
				"message": _("Student {0} with DOB {1} already exists. Please resolve this duplicate manually.").format(
					student_row.get("student_name"), student_row.get("student_dob")
				),
				"matches": identity_matches,
			})
		counts["students_to_create"] += 1
	return {
		"row": student_row.get("row_number"),
		"student_name": student_row.get("student_name"),
		"student_dob": student_row.get("student_dob"),
		"student_status": student_row.get("student_status"),
		"student": matches[0] if len(matches) == 1 else None,
		"counts": dict(counts),
		"errors": errors,
	}


def _run_parent_record(parent_record):
	counts = defaultdict(int)
	email = parent_record.get("email")
	user = _ensure_user(email, parent_record.get("parent_name"))
	if user.get("created"):
		counts["users_created"] += 1
	else:
		counts["users_reused"] += 1

	parent_matches = _find_parent_matches(email)
	if len(parent_matches) > 1:
		frappe.throw(_("Multiple Parent records match email {0}.").format(email))
	customer = _find_customer_for_parent(email, parent_matches)
	parent = _ensure_parent(parent_record, user.get("name"), customer)
	if parent.get("created"):
		counts["parents_created"] += 1
	else:
		counts["parents_updated"] += 1 if parent.get("updated") else 0
		counts["parents_reused"] += 0 if parent.get("updated") else 1

	customer_name = ensure_parent_customer(parent.get("name"))
	if customer_name == customer:
		counts["customers_reused"] += 1
	elif customer:
		counts["customers_reused"] += 1
	else:
		counts["customers_created"] += 1

	students = []
	for student_row in parent_record.get("students") or []:
		student_result = _ensure_student(parent.get("name"), student_row)
		students.append(student_result)
		if student_result.get("created"):
			counts["students_created"] += 1
		elif student_result.get("updated"):
			counts["students_updated"] += 1
		else:
			counts["students_reused"] += 1

	warnings = []
	if flt(parent_record.get("legacy_balance")):
		warnings.append({
			"field": "Balance",
			"message": _("Legacy balance {0} was detected but was not imported. Use store credit opening balance import.").format(parent_record.get("legacy_balance")),
		})

	return {
		"parent_email": email,
		"parent_status": parent_record.get("parent_status"),
		"user": user.get("name"),
		"customer": customer_name,
		"parent": parent.get("name"),
		"students": students,
		"counts": dict(counts),
		"warnings": warnings,
	}


def _ensure_user(email, parent_name):
	user_name = _find_user(email)
	if not user_name:
		user_doc = frappe.new_doc("User")
		user_doc.email = email
		user_doc.first_name = parent_name or email
		user_doc.enabled = 1
		user_doc.user_type = "Website User"
		user_doc.send_welcome_email = 0
		if frappe.db.exists("Role", "Parent"):
			user_doc.append("roles", {"role": "Parent"})
		user_doc.flags.ignore_permissions = True
		user_doc.insert(ignore_permissions=True)
		return {"name": user_doc.name, "created": True}

	user_doc = frappe.get_doc("User", user_name)
	changed = False
	if not user_doc.get("enabled"):
		user_doc.enabled = 1
		changed = True
	if user_doc.get("user_type") != "Website User":
		user_doc.user_type = "Website User"
		changed = True
	if frappe.db.exists("Role", "Parent"):
		roles = {row.role for row in user_doc.get("roles", []) if row.get("role")}
		if "Parent" not in roles:
			user_doc.append("roles", {"role": "Parent"})
			changed = True
	if changed:
		user_doc.flags.ignore_permissions = True
		user_doc.save(ignore_permissions=True)
	return {"name": user_doc.name, "created": False, "updated": changed}


def _ensure_parent(parent_record, user, customer):
	email = parent_record.get("email")
	matches = _find_parent_matches(email)
	if matches:
		doc = frappe.get_doc("Parent", matches[0])
		created = False
	else:
		doc = frappe.new_doc("Parent")
		created = True

	changed = False
	if not doc.get("parent_name"):
		doc.parent_name = parent_record.get("parent_name") or email
		changed = True
	if doc.meta.has_field("linked_user") and doc.get("linked_user") != user:
		doc.linked_user = user
		changed = True
	if parent_record.get("parent_mobile") and doc.meta.has_field("mobile_number") and not doc.get("mobile_number"):
		doc.mobile_number = parent_record.get("parent_mobile")
		changed = True
	for fieldname in ("email", "email_id", "contact_email"):
		if email and doc.meta.has_field(fieldname) and not doc.get(fieldname):
			doc.set(fieldname, email)
			changed = True
	if parent_record.get("parent_status") and doc.meta.has_field("status") and doc.get("status") != parent_record.get("parent_status"):
		doc.status = parent_record.get("parent_status")
		changed = True
	if customer and doc.meta.has_field("customer") and doc.get("customer") != customer:
		doc.customer = customer
		changed = True

	if created:
		doc.insert(ignore_permissions=True)
	else:
		if changed:
			doc.save(ignore_permissions=True)
	status_changed = _force_parent_status(doc.name, parent_record.get("parent_status"))
	return {"name": doc.name, "created": created, "updated": (changed or status_changed) and not created}


def _ensure_student(parent, student_row):
	matches = _find_student_matches(parent, student_row)
	if len(matches) > 1:
		frappe.throw(_("Multiple Student records match row {0}.").format(student_row.get("row_number")))
	if matches:
		doc = frappe.get_doc("Student", matches[0])
		created = False
	else:
		identity_matches = _find_student_identity_matches(student_row)
		if identity_matches:
			frappe.throw(
				_("Student {0} with DOB {1} already exists: {2}").format(
					student_row.get("student_name"),
					student_row.get("student_dob"),
					", ".join(identity_matches),
				)
			)
		doc = frappe.new_doc("Student")
		created = True

	parent_field = _student_parent_field()
	changed = False
	if not doc.get("student_name"):
		doc.student_name = student_row.get("student_name")
		changed = True
	if parent_field and doc.get(parent_field) != parent:
		doc.set(parent_field, parent)
		changed = True
	if student_row.get("student_dob") and doc.meta.has_field("date_of_birth") and not doc.get("date_of_birth"):
		doc.date_of_birth = student_row.get("student_dob")
		changed = True
	if student_row.get("student_status") and doc.meta.has_field("status") and doc.get("status") != student_row.get("student_status"):
		doc.status = student_row.get("student_status")
		changed = True
	if created and doc.meta.has_field("status") and not doc.get("status"):
		doc.status = student_row.get("student_status") or ACTIVE_STUDENT_STATUS
	if created:
		doc.student_name = student_row.get("student_name")
		doc.insert(ignore_permissions=True)
	else:
		if changed:
			doc.save(ignore_permissions=True)
	return {
		"row": student_row.get("row_number"),
		"student": doc.name,
		"student_name": doc.get("student_name"),
		"created": created,
		"updated": changed and not created,
	}


def _find_parent_matches(email):
	matches = []
	user = _find_user(email)
	if user and _has_field("Parent", "linked_user"):
		matches.extend(frappe.get_all("Parent", filters={"linked_user": user}, pluck="name"))
	if _has_field("Parent", "linked_user"):
		matches.extend(frappe.get_all("Parent", filters={"linked_user": email}, pluck="name"))
	for fieldname in ("email", "email_id", "contact_email"):
		if _has_field("Parent", fieldname):
			matches.extend(frappe.get_all("Parent", filters={fieldname: email}, pluck="name"))
	if email:
		matches.extend(frappe.get_all("Parent", filters={"name": email}, pluck="name"))
		matches.extend(frappe.get_all("Parent", filters={"name": ["like", f"%{email}%"]}, pluck="name"))
	return _unique(matches)


def _find_customer_for_parent(email, parent_matches=None):
	for parent in parent_matches or []:
		if _has_field("Parent", "customer"):
			customer = frappe.db.get_value("Parent", parent, "customer")
			if customer and frappe.db.exists("Customer", customer):
				return customer
	return _find_customer_by_email(email)


def _find_customer_by_email(email):
	if not _doctype_available("Customer"):
		return None
	for fieldname in ("email_id", "email", "contact_email"):
		if _has_field("Customer", fieldname):
			customer = frappe.db.get_value("Customer", {fieldname: email}, "name")
			if customer:
				return customer
	return None


def _find_user(email):
	if not email:
		return None
	return frappe.db.exists("User", email) or frappe.db.get_value("User", {"email": email}, "name")


def _find_student_matches(parent, student_row):
	if not parent or not _doctype_available("Student"):
		return []
	parent_field = _student_parent_field()
	if not parent_field:
		return []
	matches = []
	if not student_row.get("student_name") or not _has_field("Student", "student_name"):
		return []

	rows = frappe.get_all(
		"Student",
		filters={parent_field: parent},
		fields=["name", "student_name", "date_of_birth"],
		limit_page_length=0,
	)
	target_name = _normalized_key(student_row.get("student_name"))
	target_dob = student_row.get("student_dob")
	for row in rows:
		if _normalized_key(row.get("student_name")) != target_name:
			continue
		if target_dob and str(row.get("date_of_birth") or "") != target_dob:
			continue
		matches.append(row.name)
	return _unique(matches)


def _find_student_identity_matches(student_row):
	if not _doctype_available("Student"):
		return []
	if not student_row.get("student_name") or not student_row.get("student_dob"):
		return []
	if not _has_field("Student", "student_name") or not _has_field("Student", "date_of_birth"):
		return []

	rows = frappe.get_all(
		"Student",
		filters={"date_of_birth": student_row.get("student_dob")},
		fields=["name", "student_name"],
		limit_page_length=0,
	)
	target_name = _normalized_key(student_row.get("student_name"))
	return _unique([row.name for row in rows if _normalized_key(row.get("student_name")) == target_name])


def _student_parent_field():
	for fieldname in ("guardian", "parent"):
		if _has_field("Student", fieldname):
			return fieldname
	return None


def _empty_result(dry_run):
	return {
		"ok": True,
		"dry_run": dry_run,
		"input": {},
		"counts": defaultdict(int),
		"parents": [],
		"errors": [],
		"warnings": [],
	}


def _finalize_result(result):
	result["counts"] = dict(result.get("counts") or {})
	return result


def _accumulate_counts(target, source):
	for key, value in (source or {}).items():
		target[key] += value


def _row_error(row_number, email, field, message):
	return {
		"row_number": row_number,
		"parent_email": email,
		"errors": [_field_error(field, message)],
	}


def _field_error(field, message):
	return {"field": field, "message": str(message)}


def _get_payload(payload):
	if payload is None:
		request = getattr(frappe.local, "request", None)
		if request:
			payload = request.form.get("payload") or request.get_data(as_text=True)
	if isinstance(payload, str):
		payload = payload.strip()
		if not payload:
			return {}
		try:
			return json.loads(payload)
		except Exception:
			frappe.throw(_("Payload must be valid JSON."))
	return payload or {}


def _first(row, keys):
	for key in keys:
		value = row.get(key)
		if value not in (None, ""):
			return value
	return ""


def _clean_text(value):
	if value is None:
		return ""
	return str(value).replace("\ufeff", "").strip()


def _normalize_spaces(value):
	return re.sub(r"\s+", " ", (value or "").strip())


def _normalize_email(value):
	return (value or "").strip().lower()


def _normalize_phone(value):
	value = (value or "").strip()
	if not value:
		return ""
	candidates = re.split(r"[\n\r\t,;/]+|\s{2,}", value)
	for candidate in candidates:
		phone = _normalize_single_phone(candidate)
		if phone:
			return phone
	return _normalize_single_phone(value)


def _normalize_single_phone(value):
	value = (value or "").strip()
	if not value:
		return ""
	if value.startswith("+"):
		digits = re.sub(r"\D+", "", value)
		return f"+{digits}" if 8 <= len(digits) <= 15 else ""
	digits = re.sub(r"\D+", "", value)
	if len(digits) == 10 and digits.startswith("0"):
		return "+61" + digits[1:]
	if digits.startswith("61"):
		return "+" + digits
	return ""


def _normalize_student_status(value):
	value = _normalize_spaces(value)
	if not value:
		return ACTIVE_STUDENT_STATUS
	if value.lower() == "inactive":
		return INACTIVE_STUDENT_STATUS
	return ACTIVE_STUDENT_STATUS


def _normalize_parent_status(value):
	value = _normalize_spaces(value)
	if not value:
		return ACTIVE_PARENT_STATUS
	if value.lower() == "inactive":
		return INACTIVE_PARENT_STATUS
	return ACTIVE_PARENT_STATUS


def _parse_date(value):
	value = (value or "").strip()
	if not value:
		return ""
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y"):
		try:
			return datetime.strptime(value, fmt).date().isoformat()
		except Exception:
			pass
	try:
		return str(getdate(value))
	except Exception:
		return ""


def _money(value):
	value = (value or "").strip()
	if not value:
		return 0
	value = value.replace("$", "").replace(",", "")
	return flt(value)


def _normalized_key(value):
	return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _unique(values):
	return list(dict.fromkeys([value for value in values if value]))


def _has_field(doctype, fieldname):
	return _doctype_available(doctype) and frappe.db.has_column(doctype, fieldname)


def _doctype_available(doctype):
	return frappe.db.exists("DocType", doctype)


def _force_parent_status(parent, status):
	if not parent or not status or not _has_field("Parent", "status"):
		return False
	current = frappe.db.get_value("Parent", parent, "status")
	if current == status:
		return False
	frappe.db.set_value("Parent", parent, "status", status, update_modified=False)
	return True


def _restore_flag(key, value):
	if value is None:
		frappe.flags.pop(key, None)
	else:
		frappe.flags[key] = value
