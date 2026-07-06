from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import re

import frappe
from frappe import _
from frappe.utils import flt, getdate

from qas_custom.modules.billing.store_credit import LEDGER_DOCTYPE, adjust_store_credit
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
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
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
