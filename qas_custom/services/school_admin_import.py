from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

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
from qas_custom.services.school_admin import (
	_add_comment as _school_admin_add_comment,
	_clear_deleted_invoice_enrollment_snapshot,
	_existing_invoice_for_enrollment,
	_mark_draft_invoice_cancelled,
	_require_school_admin,
	cancel_school_admin_invoice_data,
)
from qas_custom.utils.environment import payment_block_reason, payment_mutations_enabled


ACTIVE_STUDENT_STATUS = "Active"
INACTIVE_STUDENT_STATUS = "Inactive"
ACTIVE_PARENT_STATUS = "Active"
INACTIVE_PARENT_STATUS = "Inactive"
ENROLLMENT_CANCELLATION_IMPORT_TYPE = "Enrollment Cancellation"
ENROLLMENT_CANCELLATION_ALLOWED_STATUSES = {"Planned", "Active"}
ENROLLMENT_CHANGE_REPORT_TYPE = "Enrollment Change"
INVOICE_ENROLLMENT_RESET_REPORT_TYPE = "Invoice Enrollment Reset"
INVOICE_ENROLLMENT_RESET_MODE_CHANGE = "change"
INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW = "withdraw"
INVOICE_ENROLLMENT_RESET_MODES = {
	INVOICE_ENROLLMENT_RESET_MODE_CHANGE,
	INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW,
}
INVOICE_ENROLLMENT_RESET_PREVIEW_TTL_SECONDS = 15 * 60
ENROLLMENT_CHANGE_CANCEL_ENROLLMENT = "cancel_enrollment"
ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE = "reset_for_class_change"
ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY = "reissue_invoice_only"
ENROLLMENT_CHANGE_ACTIONS = {
	ENROLLMENT_CHANGE_CANCEL_ENROLLMENT,
	ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE,
	ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY,
}
CANCELLABLE_ATTENDANCE_STATUSES = ["To be started", "Scheduled"]
HISTORICAL_ATTENDANCE_BLOCKING_STATUSES = ["Present", "Late"]
OPERATION_REPORT_DOCTYPE = "QAS Operation Report"
OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_IMPORT = "School Admin Import"
OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_ENROLLMENT_CHANGE = "School Admin Enrollment Change"
OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_INVOICE_ENROLLMENT_RESET = "School Admin Invoice Enrollment Reset"


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


def preview_enrollment_cancellation_import_data(payload=None):
	_require_school_admin()
	batch = _build_enrollment_cancellation_import_batch(payload)
	return _preview_enrollment_cancellation_batch(batch)


def run_enrollment_cancellation_import_data(payload=None):
	_require_school_admin()
	started_at = now_datetime()
	batch = _build_enrollment_cancellation_import_batch(payload)
	preview = _preview_enrollment_cancellation_batch(batch)
	if preview.get("blocking_error_count"):
		result = {
			"ok": False,
			"dry_run": False,
			"message": _("Enrollment cancellation import has blocking errors. Run preview and fix the CSV first."),
			"preview": preview,
		}
		result["report"] = _save_operation_report(
			report_type=ENROLLMENT_CANCELLATION_IMPORT_TYPE,
			status="Blocked",
			result=preview,
			source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_IMPORT,
			started_at=started_at,
			source_filename=batch.get("source_filename"),
		)
		return result

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		for row in batch.get("cancellations", []):
			savepoint = f"qas_enrollment_cancel_import_{frappe.generate_hash(length=10)}"
			frappe.db.savepoint(savepoint)
			try:
				row_result = _run_enrollment_cancellation_row(row)
				result["parents"].append(row_result)
				_accumulate_counts(result["counts"], row_result.get("counts") or {})
				result["warnings"].extend(row_result.get("warnings") or [])
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				result["errors"].append({
					"row": row.get("row_number"),
					"parent_email": row.get("parent_email"),
					"field": "enrollment_cancellation",
					"message": str(exc),
				})
				result["parents"].append(_enrollment_cancellation_error_payload(row, exc))
				result["counts"]["enrollment_cancellation_errors"] += 1
				frappe.log_error(frappe.get_traceback(), "QAS enrollment cancellation import failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	status = "Completed With Errors" if result["errors"] else "Completed"
	result = _finalize_result(result)
	result["report"] = _save_operation_report(
		report_type=ENROLLMENT_CANCELLATION_IMPORT_TYPE,
		status=status,
		result=result,
		source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_IMPORT,
		started_at=started_at,
		source_filename=batch.get("source_filename"),
	)
	return result


def preview_enrollment_change_data(payload=None):
	_require_school_admin()
	operation = _build_enrollment_change_operation(payload)
	return _preview_enrollment_change(operation)


def run_enrollment_change_data(payload=None):
	_require_school_admin()
	started_at = now_datetime()
	operation = _build_enrollment_change_operation(payload)
	preview = _preview_enrollment_change(operation)
	if preview.get("blocking_error_count"):
		result = {
			"ok": False,
			"dry_run": False,
			"message": _("Enrollment change has blocking errors. Run preview and fix the action first."),
			"preview": preview,
		}
		result["report"] = _save_operation_report(
			report_type=ENROLLMENT_CHANGE_REPORT_TYPE,
			status="Blocked",
			result=preview,
			source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_ENROLLMENT_CHANGE,
			source_reference=operation.get("row", {}).get("enrollment"),
			started_at=started_at,
		)
		return result

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		savepoint = f"qas_enrollment_change_{frappe.generate_hash(length=10)}"
		frappe.db.savepoint(savepoint)
		try:
			row_result = _run_enrollment_change_operation(operation.get("row") or {})
			result["parents"].append(row_result)
			_accumulate_counts(result["counts"], row_result.get("counts") or {})
			result["warnings"].extend(row_result.get("warnings") or [])
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			row = operation.get("row") or {}
			result["errors"].append({
				"row": row.get("row_number"),
				"parent_email": row.get("parent_email"),
				"field": "enrollment_change",
				"message": str(exc),
			})
			result["parents"].append(_enrollment_change_error_payload(row, exc))
			result["counts"]["enrollment_change_errors"] += 1
			frappe.log_error(frappe.get_traceback(), "QAS enrollment change failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	status = "Completed With Errors" if result["errors"] else "Completed"
	result = _finalize_result(result)
	result["report"] = _save_operation_report(
		report_type=ENROLLMENT_CHANGE_REPORT_TYPE,
		status=status,
		result=result,
		source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_ENROLLMENT_CHANGE,
		source_reference=operation.get("row", {}).get("enrollment"),
		started_at=started_at,
		success_count=_enrollment_change_success_count(result),
	)
	return result


def preview_invoice_enrollment_reset_data(payload=None):
	_require_school_admin()
	operation = _build_invoice_enrollment_reset_operation(payload)
	preview = _preview_invoice_enrollment_reset(operation)
	if preview.get("ok"):
		preview["preview_fingerprint"] = _store_invoice_enrollment_reset_preview(operation.get("row") or {}, preview)
	return preview


def run_invoice_enrollment_reset_data(payload=None):
	_require_school_admin()
	started_at = now_datetime()
	operation = _build_invoice_enrollment_reset_operation(payload)
	preview = _preview_invoice_enrollment_reset(operation)
	source_reference = operation.get("row", {}).get("invoice")
	row = operation.get("row") or {}
	if not preview.get("blocking_error_count"):
		preview_error = _validate_invoice_enrollment_reset_preview(row, preview)
		if preview_error:
			preview["errors"].append(_invoice_enrollment_reset_issue(row, "preview", preview_error))
		if _invoice_enrollment_reset_requires_multiple_withdrawal_confirmation(row, preview):
			preview["errors"].append(_invoice_enrollment_reset_issue(
				row,
				"confirm_multiple_withdrawal",
				_("Confirm withdrawal for all {0} affected students before continuing.").format(
					(preview.get("input") or {}).get("student_count", 0)
				),
			))
		preview["blocking_error_count"] = len(preview.get("errors") or [])
		preview["ok"] = preview["blocking_error_count"] == 0
	if preview.get("blocking_error_count"):
		result = {
			"ok": False,
			"dry_run": False,
			"message": _("Invoice reset has blocking errors. Run preview and fix the action first."),
			"preview": preview,
		}
		result["report"] = _save_operation_report(
			report_type=INVOICE_ENROLLMENT_RESET_REPORT_TYPE,
			status="Blocked",
			result=preview,
			source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_INVOICE_ENROLLMENT_RESET,
			source_reference=source_reference,
			started_at=started_at,
		)
		return result

	result = _empty_result(dry_run=False)
	result["input"] = preview.get("input")
	previous_in_import = getattr(frappe.flags, "in_import", None)
	previous_mute_emails = getattr(frappe.flags, "mute_emails", None)
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	try:
		savepoint = f"qas_invoice_reset_{frappe.generate_hash(length=10)}"
		frappe.db.savepoint(savepoint)
		try:
			run_result = _run_invoice_enrollment_reset_operation(operation.get("row") or {}, preview)
			result["parents"].extend(run_result.get("parents") or [])
			_accumulate_counts(result["counts"], run_result.get("counts") or {})
			result["warnings"].extend(run_result.get("warnings") or [])
			result["invoice"] = run_result.get("invoice")
			result["invoice_status"] = run_result.get("invoice_status")
			result["invoice_action"] = run_result.get("invoice_action")
			result["invoice_message"] = run_result.get("invoice_message")
			result["message"] = run_result.get("message")
			frappe.db.commit()
			frappe.cache().delete_value(_invoice_enrollment_reset_preview_cache_key(row.get("preview_fingerprint")))
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			row = operation.get("row") or {}
			result["errors"].append({
				"row": row.get("row_number"),
				"field": "invoice_reset",
				"message": str(exc),
			})
			result["parents"].append(_invoice_enrollment_reset_error_payload(row, exc))
			result["counts"]["invoice_reset_errors"] += 1
			frappe.log_error(frappe.get_traceback(), "QAS invoice enrollment reset failed")
	finally:
		_restore_flag("in_import", previous_in_import)
		_restore_flag("mute_emails", previous_mute_emails)

	result["ok"] = not result["errors"]
	result["error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	status = "Completed With Errors" if result["errors"] else "Completed"
	result = _finalize_result(result)
	result["report"] = _save_operation_report(
		report_type=INVOICE_ENROLLMENT_RESET_REPORT_TYPE,
		status=status,
		result=result,
		source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_INVOICE_ENROLLMENT_RESET,
		source_reference=source_reference,
		started_at=started_at,
		success_count=_invoice_enrollment_reset_success_count(result),
	)
	return result


def get_import_runs_data(import_type=None, limit=20):
	return get_operation_reports_data(
		report_type=_report_type_label(import_type),
		source=OPERATION_REPORT_SOURCE_SCHOOL_ADMIN_IMPORT,
		limit=limit,
	)


def get_import_run_data(import_run=None):
	return get_operation_report_data(operation_report=import_run)


def get_operation_reports_data(report_type=None, source=None, limit=20):
	_require_school_admin()
	if not _doctype_available(OPERATION_REPORT_DOCTYPE):
		return {"items": []}
	filters = {}
	if report_type:
		filters["report_type"] = _report_type_label(report_type)
	if source:
		filters["source"] = source
	rows = frappe.get_all(
		OPERATION_REPORT_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"report_type",
			"source",
			"source_reference",
			"status",
			"source_filename",
			"started_at",
			"finished_at",
			"input_row_count",
			"success_count",
			"error_count",
			"warning_count",
			"manual_action_count",
		],
		order_by="finished_at desc, creation desc",
		limit=_limit(limit, default=20, max_value=100),
	)
	items = []
	for row in rows:
		item = dict(row)
		item["import_type"] = item.get("report_type")
		items.append(item)
	return {"items": items}


def get_operation_report_data(operation_report=None):
	_require_school_admin()
	if not operation_report:
		frappe.throw(_("Operation report is required."))
	if not frappe.db.exists(OPERATION_REPORT_DOCTYPE, operation_report):
		frappe.throw(_("Operation report {0} was not found.").format(operation_report))
	doc = frappe.get_doc(OPERATION_REPORT_DOCTYPE, operation_report)
	report = _decode_report_json(doc.get("report_json")) or {}
	return {
		"name": doc.name,
		"report_type": doc.get("report_type"),
		"import_type": doc.get("report_type"),
		"source": doc.get("source"),
		"source_reference": doc.get("source_reference"),
		"status": doc.get("status"),
		"source_filename": doc.get("source_filename"),
		"started_at": doc.get("started_at"),
		"finished_at": doc.get("finished_at"),
		"input_row_count": doc.get("input_row_count"),
		"success_count": doc.get("success_count"),
		"error_count": doc.get("error_count"),
		"warning_count": doc.get("warning_count"),
		"manual_action_count": doc.get("manual_action_count"),
		"report": report,
		"rows": [_operation_report_child_row_payload(row) for row in doc.get("rows") or []],
	}


def _build_enrollment_change_operation(payload=None):
	payload = _get_payload(payload)
	if not isinstance(payload, dict):
		frappe.throw(_("Enrollment change payload must be an object."))
	action = _normalize_enrollment_change_action(payload.get("action"))
	row = {
		"row_number": 1,
		"source": payload,
		"action": action,
		"action_label": _enrollment_change_action_label(action),
		"enrollment": _normalize_spaces(payload.get("enrollment")),
		"invoice": _normalize_spaces(payload.get("invoice")),
		"reason": _normalize_spaces(payload.get("reason")),
		"effective_date": _parse_date(payload.get("effective_date")) or today(),
		"source_note": _normalize_spaces(payload.get("source_note")),
		"errors": [],
	}
	if not action:
		row["errors"].append(_field_error("action", _("Action is required.")))
	elif action not in ENROLLMENT_CHANGE_ACTIONS:
		row["errors"].append(_field_error("action", _("Enrollment change action {0} is not supported.").format(action)))
	if not row["enrollment"]:
		row["errors"].append(_field_error("enrollment", _("Enrollment is required.")))
	elif not frappe.db.exists("Enrollment", row["enrollment"]):
		row["errors"].append(_field_error("enrollment", _("Enrollment {0} was not found.").format(row["enrollment"])))
	if row["invoice"] and not frappe.db.exists("Sales Invoice", row["invoice"]):
		row["errors"].append(_field_error("invoice", _("Sales Invoice {0} was not found.").format(row["invoice"])))
	if not row["reason"]:
		row["errors"].append(_field_error("reason", _("Reason is required for audit trail.")))
	return {"row": row}


def _normalize_enrollment_change_action(value):
	value = _normalized_key(value)
	aliases = {
		"cancel": ENROLLMENT_CHANGE_CANCEL_ENROLLMENT,
		"cancelenrollment": ENROLLMENT_CHANGE_CANCEL_ENROLLMENT,
		"reset": ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE,
		"resetforclasschange": ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE,
		"classchange": ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE,
		"reissue": ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY,
		"reissueinvoiceonly": ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY,
	}
	return aliases.get(value, value)


def _enrollment_change_action_label(action):
	return {
		ENROLLMENT_CHANGE_CANCEL_ENROLLMENT: "Cancel enrollment",
		ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE: "Reset for class change",
		ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY: "Reissue invoice only",
	}.get(action or "", action or "")


def _preview_enrollment_change(operation):
	result = _empty_result(dry_run=True)
	row = operation.get("row") or {}
	result["input"] = {
		"row_count": 1,
		"enrollment_count": 1 if row.get("enrollment") else 0,
		"manual_action_count": 0,
		"action": row.get("action"),
		"action_label": row.get("action_label"),
	}
	for error in row.get("errors") or []:
		result["errors"].append({"row": row.get("row_number"), "parent_email": row.get("parent_email"), **error})
	if not row.get("errors"):
		preview = _preview_enrollment_change_row(row)
		result["parents"].append(preview)
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])

	result["input"]["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = result["input"]["manual_action_count"]
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_enrollment_change_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	doc = frappe.get_doc("Enrollment", row.get("enrollment"))
	counts["enrollments_matched"] += 1
	status = doc.get("status") or ""
	resolved = {
		"doc": doc,
		"parent": doc.get("parent"),
		"student": doc.get("student"),
		"student_name": _student_name(doc.get("student"), row),
		"match_method": "enrollment",
	}

	if status == "Cancelled":
		warnings.append(_enrollment_change_issue(row, "status", _("Enrollment is already Cancelled and will be skipped.")))
		return _enrollment_change_payload(row, counts, resolved=resolved, warnings=warnings, skipped=True, message=_("Enrollment is already Cancelled."))
	if status not in ENROLLMENT_CANCELLATION_ALLOWED_STATUSES:
		errors.append(_enrollment_change_issue(row, "status", _("Only Planned or Active enrollments can use this change bench. Current status is {0}.").format(status or _("blank"))))
		return _enrollment_change_payload(row, counts, resolved=resolved, errors=errors, warnings=warnings)

	if row.get("invoice"):
		invoice_doc = frappe.get_doc("Sales Invoice", row.get("invoice"))
		if not _invoice_links_enrollment(invoice_doc, doc.name):
			errors.append(_enrollment_change_issue(row, "invoice", _("Invoice {0} is not linked to enrollment {1}.").format(row.get("invoice"), doc.name)))
			return _enrollment_change_payload(row, counts, resolved=resolved, errors=errors, warnings=warnings)

	invoice_action = _classify_enrollment_cancellation_invoice(row, doc)
	_accumulate_counts(counts, invoice_action.get("counts") or {})
	warnings.extend(invoice_action.get("warnings") or [])

	action = row.get("action")
	if action == ENROLLMENT_CHANGE_CANCEL_ENROLLMENT:
		counts["enrollments_to_cancel"] += 1
		message = _("Enrollment will be cancelled.")
	elif action == ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE:
		historical_count = _count_historical_enrollment_attendance(doc.name)
		if historical_count:
			counts["historical_attendance_found"] += historical_count
			errors.append(_enrollment_change_issue(
				row,
				"attendance",
				_("Reset for class change is blocked because {0} historical attendance row(s) exist. Use a manual transfer/refund path for mid-term changes.").format(historical_count),
			))
			return _enrollment_change_payload(row, counts, resolved=resolved, invoice_action=invoice_action, errors=errors, warnings=warnings)
		counts["enrollments_to_reset_for_change"] += 1
		message = _("Enrollment will be reset to Planned for class change.")
	elif action == ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY:
		if invoice_action.get("action") in ("none", "") and not doc.get("invoice"):
			errors.append(_enrollment_change_issue(row, "invoice", _("No linked invoice was found for reissue.")))
			return _enrollment_change_payload(row, counts, resolved=resolved, invoice_action=invoice_action, errors=errors, warnings=warnings)
		counts["invoice_reissues_to_prepare"] += 1
		message = _("Invoice link will be prepared for reissue without changing enrollment status.")
	else:
		errors.append(_enrollment_change_issue(row, "action", _("Enrollment change action is not supported.")))
		return _enrollment_change_payload(row, counts, resolved=resolved, invoice_action=invoice_action, errors=errors, warnings=warnings)

	if action in (ENROLLMENT_CHANGE_CANCEL_ENROLLMENT, ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE):
		attendance_count = _count_future_enrollment_attendance(doc.name, row.get("effective_date"))
		if attendance_count:
			counts["attendance_to_cancel"] += attendance_count

	return _enrollment_change_payload(
		row,
		counts,
		resolved=resolved,
		invoice_action=invoice_action,
		warnings=warnings,
		message=message,
	)


def _run_enrollment_change_operation(row):
	preview = _preview_enrollment_change_row(row)
	if preview.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in preview.get("errors") or []))
	if preview.get("skipped"):
		return preview

	doc = frappe.get_doc("Enrollment", preview.get("enrollment"))
	action = row.get("action")
	reason = row.get("reason") or "School Admin enrollment change"
	status_before = doc.get("status")
	attendance_cancelled = 0
	counts = defaultdict(int, preview.get("counts") or {})

	if action == ENROLLMENT_CHANGE_CANCEL_ENROLLMENT:
		doc.status = "Cancelled"
		doc.save(ignore_permissions=True)
		attendance_cancelled = _cancel_future_enrollment_attendance_for_import(doc.name, effective_date=row.get("effective_date"))
		_decrement_count(counts, "enrollments_to_cancel")
		counts["enrollments_cancelled"] += 1
		_school_admin_add_comment(
			"Enrollment",
			doc.name,
			_("Enrollment cancelled by School Admin change bench from {0}. Reason: {1}").format(row.get("effective_date"), reason),
		)
	elif action == ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE:
		doc.status = "Planned"
		doc.save(ignore_permissions=True)
		attendance_cancelled = _cancel_future_enrollment_attendance_for_import(doc.name, effective_date=row.get("effective_date"))
		_decrement_count(counts, "enrollments_to_reset_for_change")
		counts["enrollments_reset_for_change"] += 1
		_school_admin_add_comment(
			"Enrollment",
			doc.name,
			_("Enrollment reset to Planned by School Admin change bench from {0}. Reason: {1}").format(row.get("effective_date"), reason),
		)
	elif action == ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY:
		_decrement_count(counts, "invoice_reissues_to_prepare")
		counts["invoice_reissues_prepared"] += 1
		_school_admin_add_comment(
			"Enrollment",
			doc.name,
			_("Enrollment invoice prepared for reissue by School Admin change bench. Reason: {0}").format(reason),
		)

	if attendance_cancelled:
		_decrement_count(counts, "attendance_to_cancel", attendance_cancelled)
		counts["attendance_cancelled"] += attendance_cancelled

	invoice_action = preview.get("invoice_action_detail") or {}
	invoice_result = _apply_enrollment_change_invoice_action(invoice_action, reason)
	_mark_invoice_action_completed_in_counts(counts, invoice_action.get("action"))
	if invoice_result.get("count_key"):
		counts[invoice_result.get("count_key")] += 1
	if invoice_action.get("action") in ("none", "already_cancelled"):
		snapshot_cleared = _clear_enrollment_change_invoice_snapshot(
			doc.name,
			invoice_action.get("invoice") or preview.get("invoice") or doc.get("invoice"),
			reason,
		)
		if snapshot_cleared:
			counts["invoice_snapshots_cleared"] += snapshot_cleared

	result = {
		**preview,
		"status": _enrollment_change_result_status(action),
		"operation_status": "Completed",
		"enrollment_status_before": status_before,
		"attendance_cancelled": attendance_cancelled,
		"counts": dict(counts),
		"message": _enrollment_change_run_message(action, invoice_result, attendance_cancelled),
	}
	if invoice_result:
		result["invoice_action"] = _invoice_action_label(invoice_result.get("action") or invoice_action.get("action"))
		result["invoice_message"] = invoice_result.get("message")
		result["manual_action_required"] = invoice_result.get("manual_action_required", result.get("manual_action_required"))
		result["invoice_status"] = invoice_result.get("invoice_status") or result.get("invoice_status")
	return result


def _apply_enrollment_change_invoice_action(invoice_action, reason, allow_empty_reason=False, send_notifications=True):
	action = invoice_action.get("action")
	invoice = invoice_action.get("invoice")
	if not invoice or action in ("none", "already_cancelled"):
		return {
			"action": action or "none",
			"message": invoice_action.get("message"),
			"invoice_status": invoice_action.get("invoice_status"),
			"manual_action_required": False,
		}
	if action == "manual_adjustment":
		return {
			"action": action,
			"message": _("Invoice requires manual adjustment: {0}").format(invoice_action.get("message") or invoice),
			"invoice_status": invoice_action.get("invoice_status"),
			"manual_action_required": True,
			"count_key": "invoices_manual_adjustment_reported",
		}
	if action == "cancel_draft":
		doc = frappe.get_doc("Sales Invoice", invoice)
		_mark_draft_invoice_cancelled(doc, reason)
		_clear_deleted_invoice_enrollment_snapshot(doc, action="cancelled")
		return {
			"action": action,
			"message": _("Draft invoice was marked Cancelled."),
			"invoice_status": "Cancelled",
			"manual_action_required": False,
			"count_key": "invoices_cancelled",
		}
	if action == "cancel_submitted":
		cancellation = cancel_school_admin_invoice_data(
			invoice=invoice,
			reason=reason,
			allow_empty_reason=allow_empty_reason,
			send_notifications=send_notifications,
		)
		return {
			"action": action,
			"message": _("Submitted invoice was cancelled with the existing School Admin invoice cancellation flow."),
			"invoice_status": "Cancelled",
			"manual_action_required": False,
			"count_key": "invoices_cancelled",
			"cancellation_notification": cancellation.get("cancellation_notification"),
		}
	return {
		"action": action,
		"message": _("Invoice action was not recognized. Review manually."),
		"invoice_status": invoice_action.get("invoice_status"),
		"manual_action_required": True,
		"count_key": "invoices_manual_adjustment_reported",
	}


def _enrollment_change_payload(row, counts, resolved=None, invoice_action=None, errors=None, warnings=None, skipped=False, message=None):
	resolved = resolved or {}
	doc = resolved.get("doc")
	invoice_action = invoice_action or {}
	student = resolved.get("student") or (doc.get("student") if doc else "")
	parent = resolved.get("parent") or (doc.get("parent") if doc else "")
	invoice = invoice_action.get("invoice") or row.get("invoice") or (doc.get("invoice") if doc else "")
	manual_action = bool(invoice_action.get("manual_action_required"))
	return {
		"row": row.get("row_number"),
		"parent": parent,
		"parent_email": row.get("parent_email") or _parent_email(parent),
		"student": student,
		"student_name": _student_name(student, row),
		"student_count": 1,
		"enrollment": doc.name if doc else row.get("enrollment"),
		"enrollment_status": doc.get("status") if doc else "",
		"status": "Skipped" if skipped else (doc.get("status") if doc else ""),
		"operation_action": row.get("action_label") or _enrollment_change_action_label(row.get("action")),
		"operation_status": "Skipped" if skipped else ("Blocked" if errors else "Ready"),
		"effective_date": row.get("effective_date"),
		"reason": row.get("reason"),
		"invoice": invoice,
		"invoice_status": invoice_action.get("invoice_status"),
		"invoice_action": _invoice_action_label(invoice_action.get("action")),
		"invoice_action_detail": invoice_action,
		"manual_action_required": manual_action,
		"message": message or invoice_action.get("message") or "",
		"skipped": skipped,
		"counts": dict(counts),
		"errors": errors or [],
		"warnings": warnings or [],
		"raw_row": row.get("source"),
	}


def _enrollment_change_error_payload(row, exc):
	counts = defaultdict(int)
	counts["enrollment_change_errors"] += 1
	return {
		"row": row.get("row_number"),
		"enrollment": row.get("enrollment"),
		"invoice": row.get("invoice"),
		"operation_action": row.get("action_label") or _enrollment_change_action_label(row.get("action")),
		"operation_status": "Error",
		"status": "Error",
		"message": str(exc),
		"manual_action_required": False,
		"counts": dict(counts),
		"errors": [_enrollment_change_issue(row, "enrollment_change", str(exc))],
		"warnings": [],
		"raw_row": row.get("source"),
	}


def _enrollment_change_run_message(action, invoice_result, attendance_cancelled):
	pieces = []
	if action == ENROLLMENT_CHANGE_CANCEL_ENROLLMENT:
		pieces.append(_("Enrollment cancelled."))
	elif action == ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE:
		pieces.append(_("Enrollment reset to Planned for class change."))
	elif action == ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY:
		pieces.append(_("Enrollment invoice prepared for reissue."))
	if attendance_cancelled:
		pieces.append(_("{0} future attendance rows cancelled.").format(attendance_cancelled))
	if invoice_result.get("message"):
		pieces.append(invoice_result.get("message"))
	return " ".join(str(piece) for piece in pieces if piece)


def _enrollment_change_result_status(action):
	return {
		ENROLLMENT_CHANGE_CANCEL_ENROLLMENT: "Cancelled",
		ENROLLMENT_CHANGE_RESET_FOR_CLASS_CHANGE: "Planned",
		ENROLLMENT_CHANGE_REISSUE_INVOICE_ONLY: "Invoice Reissue",
	}.get(action or "", "Completed")


def _enrollment_change_issue(row, field, message, matches=None):
	issue = {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"field": field,
		"message": str(message),
	}
	if matches:
		issue["matches"] = matches
	return issue


def _build_invoice_enrollment_reset_operation(payload=None):
	payload = _get_payload(payload)
	if not isinstance(payload, dict):
		frappe.throw(_("Invoice reset payload must be an object."))
	row = {
		"row_number": 1,
		"source": payload,
		"invoice": _normalize_spaces(payload.get("invoice")),
		"reason": _normalize_spaces(payload.get("reason")),
		"effective_date": _parse_date(payload.get("effective_date")) or today(),
		"mode": _normalize_spaces(payload.get("mode") or INVOICE_ENROLLMENT_RESET_MODE_CHANGE).lower(),
		"preview_fingerprint": _normalize_spaces(payload.get("preview_fingerprint")),
		"confirm_multiple_withdrawal": cint(payload.get("confirm_multiple_withdrawal")),
		"send_notifications": cint(payload.get("send_notifications", 1)),
		"errors": [],
	}
	if not row["invoice"]:
		row["errors"].append(_field_error("invoice", _("Invoice is required.")))
	elif not frappe.db.exists("Sales Invoice", row["invoice"]):
		row["errors"].append(_field_error("invoice", _("Sales Invoice {0} was not found.").format(row["invoice"])))
	if row["mode"] not in INVOICE_ENROLLMENT_RESET_MODES:
		row["errors"].append(_field_error("mode", _("Invoice reset mode must be Change or Withdraw.")))
	return {"row": row}


def _invoice_enrollment_reset_requires_multiple_withdrawal_confirmation(row, preview):
	return (
		row.get("mode") == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW
		and cint((preview.get("input") or {}).get("student_count")) > 1
		and not cint(row.get("confirm_multiple_withdrawal"))
	)


def _store_invoice_enrollment_reset_preview(row, preview):
	fingerprint = frappe.generate_hash(length=32)
	frappe.cache().set_value(
		_invoice_enrollment_reset_preview_cache_key(fingerprint),
		{
			"viewer_user": frappe.session.user,
			"snapshot": _invoice_enrollment_reset_preview_snapshot(row, preview),
		},
		expires_in_sec=INVOICE_ENROLLMENT_RESET_PREVIEW_TTL_SECONDS,
	)
	return fingerprint


def _validate_invoice_enrollment_reset_preview(row, preview):
	fingerprint = row.get("preview_fingerprint")
	if not fingerprint:
		return _("Run a fresh invoice reset preview before executing this action.")
	cached_preview = frappe.cache().get_value(_invoice_enrollment_reset_preview_cache_key(fingerprint))
	if not cached_preview or cached_preview.get("viewer_user") != frappe.session.user:
		return _("This invoice reset preview has expired. Run preview again before executing.")
	if cached_preview.get("snapshot") != _invoice_enrollment_reset_preview_snapshot(row, preview):
		return _("The invoice or linked enrollments changed after preview. Run preview again before executing.")
	return ""


def _invoice_enrollment_reset_preview_cache_key(fingerprint):
	return f"qas:invoice-enrollment-reset-preview:{fingerprint}"


def _invoice_enrollment_reset_preview_snapshot(row, preview):
	return {
		"invoice": row.get("invoice"),
		"reason": row.get("reason"),
		"effective_date": str(row.get("effective_date") or ""),
		"send_notifications": cint(row.get("send_notifications", 1)),
		"invoice_status": preview.get("invoice_status"),
		"invoice_action": preview.get("invoice_action"),
		"student_count": (preview.get("input") or {}).get("student_count", 0),
		"enrollments": [
			{
				"enrollment": item.get("enrollment"),
				"student": item.get("student"),
				"enrollment_status": item.get("enrollment_status"),
				"skipped": bool(item.get("skipped")),
				"attendance_to_cancel": (item.get("counts") or {}).get("attendance_to_cancel", 0),
			}
			for item in preview.get("parents") or []
		],
	}


def _invoice_enrollment_reset_action_label(mode):
	return "Withdraw enrollment(s)" if mode == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW else "Change enrollment"


def _invoice_enrollment_reset_target_status(mode):
	return "Cancelled" if mode == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW else "Planned"


def _invoice_enrollment_reset_pending_count_key(mode):
	return "enrollments_to_withdraw" if mode == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW else "enrollments_to_reset_for_change"


def _invoice_enrollment_reset_completed_count_key(mode):
	return "enrollments_withdrawn" if mode == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW else "enrollments_reset_for_change"


def _preview_invoice_enrollment_reset(operation):
	result = _empty_result(dry_run=True)
	row = operation.get("row") or {}
	result["input"] = {
		"row_count": 1,
		"invoice": row.get("invoice"),
		"invoice_count": 1 if row.get("invoice") else 0,
		"enrollment_count": 0,
		"student_count": 0,
		"manual_action_count": 0,
		"action": row.get("mode") or INVOICE_ENROLLMENT_RESET_MODE_CHANGE,
		"action_label": _invoice_enrollment_reset_action_label(row.get("mode")),
		"send_notifications": cint(row.get("send_notifications", 1)),
	}
	for error in row.get("errors") or []:
		result["errors"].append({"row": row.get("row_number"), **error})
	if not row.get("errors"):
		preview = _preview_invoice_enrollment_reset_row(row)
		result["parents"].extend(preview.get("parents") or [])
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])
		result["invoice"] = preview.get("invoice")
		result["invoice_status"] = preview.get("invoice_status")
		result["invoice_action"] = preview.get("invoice_action")
		result["invoice_action_detail"] = preview.get("invoice_action_detail")
		result["message"] = preview.get("message")
		result["input"]["enrollment_count"] = preview.get("enrollment_count") or 0
		result["input"]["student_count"] = preview.get("student_count") or 0

	result["input"]["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = result["input"]["manual_action_count"]
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_invoice_enrollment_reset_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	invoice = row.get("invoice")
	invoice_doc = frappe.get_doc("Sales Invoice", invoice)
	counts["invoices_matched"] += 1
	invoice_action = _classify_invoice_enrollment_reset_invoice(row, invoice_doc)
	_accumulate_counts(counts, invoice_action.get("counts") or {})
	errors.extend(invoice_action.get("errors") or [])
	warnings.extend(invoice_action.get("warnings") or [])

	enrollment_names = _invoice_reset_enrollment_names(invoice_doc)
	counts["linked_enrollments_found"] += len(enrollment_names)
	if not enrollment_names:
		errors.append(_invoice_enrollment_reset_issue(row, "invoice", _("Invoice has no linked enrollment items.")))

	parent_rows = []
	included_count = 0
	included_students = set()
	mode = row.get("mode") or INVOICE_ENROLLMENT_RESET_MODE_CHANGE
	pending_count_key = _invoice_enrollment_reset_pending_count_key(mode)
	target_status = _invoice_enrollment_reset_target_status(mode)
	for index, enrollment in enumerate(enrollment_names, start=1):
		child_row = {**row, "row_number": index, "enrollment": enrollment}
		if not frappe.db.exists("Enrollment", enrollment):
			counts["enrollments_missing"] += 1
			row_errors = [_invoice_enrollment_reset_issue(child_row, "enrollment", _("Enrollment {0} was not found.").format(enrollment))]
			errors.extend(row_errors)
			parent_rows.append(_invoice_enrollment_reset_payload(child_row, counts, invoice_doc=invoice_doc, errors=row_errors))
			continue

		doc = frappe.get_doc("Enrollment", enrollment)
		status = doc.get("status") or ""
		row_counts = defaultdict(int)
		if status not in ENROLLMENT_CANCELLATION_ALLOWED_STATUSES:
			row_counts["enrollments_skipped"] += 1
			row_warnings = [_invoice_enrollment_reset_issue(
				child_row,
				"status",
				_("Enrollment {0} is {1} and will not be reset.").format(enrollment, status or _("blank")),
			)]
			warnings.extend(row_warnings)
			parent_rows.append(_invoice_enrollment_reset_payload(
				child_row,
				row_counts,
				invoice_doc=invoice_doc,
				enrollment_doc=doc,
				invoice_action=invoice_action,
				warnings=row_warnings,
				skipped=True,
				message=_("Enrollment will be skipped because it is not Planned or Active."),
			))
			_accumulate_counts(counts, row_counts)
			continue

		included_count += 1
		if doc.get("student"):
			included_students.add(doc.get("student"))
		row_counts[pending_count_key] += 1
		historical_count = _count_historical_enrollment_attendance(enrollment)
		row_errors = []
		if historical_count:
			row_counts["historical_attendance_found"] += historical_count
			row_errors.append(_invoice_enrollment_reset_issue(
				child_row,
				"attendance",
				_("Invoice reset is blocked because enrollment {0} has {1} historical attendance row(s).").format(enrollment, historical_count),
			))
		attendance_count = _count_future_enrollment_attendance(enrollment, row.get("effective_date"))
		if attendance_count:
			row_counts["attendance_to_cancel"] += attendance_count
		else:
			row_counts["enrollments_with_no_future_attendance"] += 1
		errors.extend(row_errors)
		parent_rows.append(_invoice_enrollment_reset_payload(
			child_row,
			row_counts,
			invoice_doc=invoice_doc,
			enrollment_doc=doc,
			invoice_action=invoice_action,
			errors=row_errors,
			message=_("Enrollment will be {0} with the selected invoice.").format(target_status),
		))
		_accumulate_counts(counts, row_counts)

	if not included_count and enrollment_names:
		errors.append(_invoice_enrollment_reset_issue(row, "enrollment", _("No linked Planned or Active enrollments can be reset.")))

	return {
		"invoice": invoice,
		"invoice_status": invoice_action.get("invoice_status"),
		"invoice_action": _invoice_action_label(invoice_action.get("action")),
		"invoice_action_detail": invoice_action,
		"enrollment_count": included_count,
		"student_count": len(included_students),
		"parents": parent_rows,
		"counts": counts,
		"errors": errors,
		"warnings": warnings,
		"message": _("Invoice reset preview is ready. Choose Change or Withdraw after reviewing the affected enrollments."),
	}


def _run_invoice_enrollment_reset_operation(row, preview):
	invoice_action = preview.get("invoice_action_detail") or {}
	reason = row.get("reason") or ""
	mode = row.get("mode") or INVOICE_ENROLLMENT_RESET_MODE_CHANGE
	target_status = _invoice_enrollment_reset_target_status(mode)
	pending_count_key = _invoice_enrollment_reset_pending_count_key(mode)
	completed_count_key = _invoice_enrollment_reset_completed_count_key(mode)
	invoice_result = _apply_enrollment_change_invoice_action(
		invoice_action,
		reason,
		allow_empty_reason=True,
		send_notifications=row.get("send_notifications", 1),
	)
	result = {
		"invoice": row.get("invoice"),
		"invoice_status": invoice_result.get("invoice_status") or preview.get("invoice_status"),
		"invoice_action": _invoice_action_label(invoice_result.get("action") or invoice_action.get("action")),
		"invoice_message": invoice_result.get("message"),
		"parents": [],
		"counts": defaultdict(int, preview.get("counts") or {}),
		"warnings": list(preview.get("warnings") or []),
	}
	if invoice_result.get("cancellation_notification") is not None:
		result["cancellation_notification"] = invoice_result.get("cancellation_notification")
	_mark_invoice_action_completed_in_counts(result["counts"], invoice_action.get("action"))
	if invoice_result.get("count_key"):
		result["counts"][invoice_result.get("count_key")] += 1

	for preview_row in preview.get("parents") or []:
		if preview_row.get("skipped"):
			result["parents"].append(preview_row)
			continue
		enrollment = preview_row.get("enrollment")
		doc = frappe.get_doc("Enrollment", enrollment)
		status_before = doc.get("status")
		doc.status = target_status
		doc.save(ignore_permissions=True)
		attendance_cancelled = _cancel_future_enrollment_attendance_for_import(
			enrollment,
			effective_date=row.get("effective_date"),
		)
		snapshot_cleared = _clear_enrollment_change_invoice_snapshot(enrollment, row.get("invoice"), reason)
		_decrement_count(result["counts"], pending_count_key)
		_decrement_count(result["counts"], "attendance_to_cancel", attendance_cancelled)
		if snapshot_cleared:
			result["counts"]["invoice_snapshots_cleared"] += snapshot_cleared
		result["counts"][completed_count_key] += 1
		if attendance_cancelled:
			result["counts"]["attendance_cancelled"] += attendance_cancelled
		comment = _("Enrollment set to {0} by School Admin invoice reset from {1}. Invoice: {2}.").format(
			target_status,
			row.get("effective_date"),
			row.get("invoice"),
		)
		if reason:
			comment = _("{0} Reason: {1}").format(comment, reason)
		_school_admin_add_comment("Enrollment", enrollment, comment)
		message_parts = [_("Enrollment set to {0}.").format(target_status)]
		if attendance_cancelled:
			message_parts.append(_("{0} future attendance rows cancelled.").format(attendance_cancelled))
		if invoice_result.get("message"):
			message_parts.append(invoice_result.get("message"))
		result["parents"].append({
			**preview_row,
			"status": target_status,
			"operation_status": "Completed",
			"enrollment_status_before": status_before,
			"attendance_cancelled": attendance_cancelled,
			"invoice_action": result.get("invoice_action"),
			"invoice_message": invoice_result.get("message"),
			"invoice_status": result.get("invoice_status"),
			"message": " ".join(str(part) for part in message_parts if part),
			"counts": {
				completed_count_key: 1,
				"attendance_cancelled": attendance_cancelled,
				"invoice_snapshots_cleared": snapshot_cleared,
			},
		})

	result["message"] = _invoice_enrollment_reset_run_message(result, invoice_result, mode)
	return result


def _classify_invoice_enrollment_reset_invoice(row, invoice_doc):
	counts = defaultdict(int)
	warnings = []
	errors = []
	invoice_status = _invoice_status_for_report(invoice_doc)
	if cint(invoice_doc.docstatus) == 2 or invoice_status == "Cancelled":
		counts["invoices_already_cancelled"] += 1
		warnings.append(_invoice_enrollment_reset_issue(row, "invoice", _("Invoice is already cancelled. Enrollments can still be reset if safe.")))
		return {
			"action": "already_cancelled",
			"invoice": invoice_doc.name,
			"invoice_status": invoice_status,
			"counts": counts,
			"warnings": warnings,
			"message": _("Invoice is already cancelled."),
		}
	if cint(invoice_doc.docstatus) == 0:
		counts["invoices_to_cancel"] += 1
		return {"action": "cancel_draft", "invoice": invoice_doc.name, "invoice_status": invoice_status, "counts": counts}
	if cint(invoice_doc.docstatus) == 1 and payment_mutations_enabled():
		counts["invoices_to_cancel"] += 1
		return {"action": "cancel_submitted", "invoice": invoice_doc.name, "invoice_status": invoice_status, "counts": counts}

	message = payment_block_reason() if cint(invoice_doc.docstatus) == 1 else _("Invoice docstatus is not supported for automatic reset.")
	errors.append(_invoice_enrollment_reset_issue(row, "invoice", _("Invoice {0} cannot be reset automatically: {1}").format(invoice_doc.name, message)))
	return {
		"action": "blocked",
		"invoice": invoice_doc.name,
		"invoice_status": invoice_status,
		"counts": counts,
		"errors": errors,
		"message": message,
	}


def _invoice_enrollment_reset_payload(row, counts, invoice_doc=None, enrollment_doc=None, invoice_action=None, errors=None, warnings=None, skipped=False, message=None):
	invoice_action = invoice_action or {}
	parent = enrollment_doc.get("parent") if enrollment_doc else (invoice_doc.get("parent") if invoice_doc else "")
	student = enrollment_doc.get("student") if enrollment_doc else ""
	return {
		"row": row.get("row_number"),
		"parent": parent,
		"parent_email": _parent_email(parent),
		"student": student,
		"student_name": _student_name(student, row),
		"student_count": 1 if student else 0,
		"enrollment": enrollment_doc.name if enrollment_doc else row.get("enrollment"),
		"enrollment_status": enrollment_doc.get("status") if enrollment_doc else "",
		"status": "Skipped" if skipped else (enrollment_doc.get("status") if enrollment_doc else ""),
		"operation_action": _invoice_enrollment_reset_action_label(row.get("mode")),
		"operation_status": "Skipped" if skipped else ("Blocked" if errors else "Ready"),
		"effective_date": row.get("effective_date"),
		"reason": row.get("reason"),
		"invoice": row.get("invoice") or (invoice_doc.name if invoice_doc else ""),
		"invoice_status": invoice_action.get("invoice_status") or (_invoice_status_for_report(invoice_doc) if invoice_doc else ""),
		"invoice_action": _invoice_action_label(invoice_action.get("action")),
		"invoice_action_detail": invoice_action,
		"manual_action_required": False,
		"message": message or invoice_action.get("message") or "",
		"skipped": skipped,
		"counts": dict(counts),
		"errors": errors or [],
		"warnings": warnings or [],
		"raw_row": row.get("source"),
	}


def _invoice_enrollment_reset_error_payload(row, exc):
	return {
		"row": row.get("row_number"),
		"invoice": row.get("invoice"),
		"operation_action": _invoice_enrollment_reset_action_label(row.get("mode")),
		"operation_status": "Error",
		"status": "Error",
		"message": str(exc),
		"manual_action_required": False,
		"counts": {"invoice_reset_errors": 1},
		"errors": [_invoice_enrollment_reset_issue(row, "invoice_reset", str(exc))],
		"warnings": [],
		"raw_row": row.get("source"),
	}


def _invoice_enrollment_reset_issue(row, field, message, matches=None):
	issue = {
		"row": row.get("row_number"),
		"field": field,
		"message": str(message),
	}
	if row.get("enrollment"):
		issue["enrollment"] = row.get("enrollment")
	if matches:
		issue["matches"] = matches
	return issue


def _invoice_reset_enrollment_names(invoice_doc):
	names = []
	for item in invoice_doc.get("items", []) or []:
		if item.get("enrollment"):
			names.append(item.get("enrollment"))
	if not names:
		if _has_field("Sales Invoice", "enrollment") and invoice_doc.get("enrollment"):
			names.append(invoice_doc.get("enrollment"))
		if (
			_has_field("Sales Invoice", "source_doctype")
			and invoice_doc.get("source_doctype") == "Enrollment"
			and invoice_doc.get("source_document")
		):
			names.append(invoice_doc.get("source_document"))
	return _unique(names)


def _invoice_enrollment_reset_run_message(result, invoice_result, mode):
	pieces = [_("Enrollment withdrawal complete.") if mode == INVOICE_ENROLLMENT_RESET_MODE_WITHDRAW else _("Invoice enrollment reset complete.")]
	completed_count = (result.get("counts") or {}).get(_invoice_enrollment_reset_completed_count_key(mode)) or 0
	attendance_count = (result.get("counts") or {}).get("attendance_cancelled") or 0
	if completed_count:
		pieces.append(_("{0} enrollment(s) set to {1}.").format(completed_count, _invoice_enrollment_reset_target_status(mode)))
	if attendance_count:
		pieces.append(_("{0} future attendance row(s) cancelled.").format(attendance_count))
	if invoice_result.get("message"):
		pieces.append(invoice_result.get("message"))
	return " ".join(str(piece) for piece in pieces if piece)


def _invoice_enrollment_reset_success_count(result):
	counts = result.get("counts") or {}
	return (counts.get("enrollments_reset_for_change") or 0) + (counts.get("enrollments_withdrawn") or 0)


def _invoice_links_enrollment(invoice_doc, enrollment):
	if not invoice_doc or not enrollment:
		return False
	if enrollment in set(_invoice_enrollment_names(invoice_doc)):
		return True
	if _has_field("Sales Invoice", "enrollment") and invoice_doc.get("enrollment") == enrollment:
		return True
	if (
		_has_field("Sales Invoice", "source_doctype")
		and _has_field("Sales Invoice", "source_document")
		and invoice_doc.get("source_doctype") == "Enrollment"
		and invoice_doc.get("source_document") == enrollment
	):
		return True
	return False


def _count_historical_enrollment_attendance(enrollment):
	if not _doctype_available(ATTENDANCE_DOCTYPE):
		return 0
	return frappe.db.count(ATTENDANCE_DOCTYPE, {
		"source_doctype": "Enrollment",
		"source_document": enrollment,
		"status": ["in", HISTORICAL_ATTENDANCE_BLOCKING_STATUSES],
	})


def _clear_enrollment_change_invoice_snapshot(enrollment, invoice, reason):
	if not enrollment or not frappe.db.exists("Enrollment", enrollment):
		return 0
	updates = {}
	current_invoice = frappe.db.get_value("Enrollment", enrollment, "invoice") if _has_field("Enrollment", "invoice") else None
	if invoice and current_invoice and current_invoice != invoice:
		return 0
	for fieldname, value in {"invoice": None, "invoice_status": None, "invoice_amount": 0}.items():
		if _has_field("Enrollment", fieldname):
			updates[fieldname] = value
	if not updates:
		return 0
	frappe.db.set_value("Enrollment", enrollment, updates, update_modified=True)
	comment = _("Invoice snapshot was cleared by School Admin change bench.")
	if reason:
		comment = _("{0} Reason: {1}").format(comment, reason)
	_school_admin_add_comment("Enrollment", enrollment, comment)
	return 1


def _parent_email(parent):
	if not parent or not frappe.db.exists("Parent", parent):
		return ""
	for fieldname in ("email", "linked_user"):
		if _has_field("Parent", fieldname):
			value = frappe.db.get_value("Parent", parent, fieldname)
			if value:
				return value
	return ""


def _decrement_count(counts, key, amount=1):
	counts[key] = max(0, counts.get(key, 0) - amount)


def _mark_invoice_action_completed_in_counts(counts, action):
	if action in ("cancel_draft", "cancel_submitted"):
		_decrement_count(counts, "invoices_to_cancel")
	elif action == "manual_adjustment":
		_decrement_count(counts, "invoices_require_manual_adjustment")


def _enrollment_change_success_count(result):
	counts = result.get("counts") or {}
	return (
		counts.get("enrollments_cancelled", 0)
		+ counts.get("enrollments_reset_for_change", 0)
		+ counts.get("invoice_reissues_prepared", 0)
	)


def _build_enrollment_cancellation_import_batch(payload=None):
	payload = _get_payload(payload)
	rows = payload.get("rows") if isinstance(payload, dict) else payload
	if not isinstance(rows, list):
		frappe.throw(_("Import rows must be a list."))
	default_effective_date = _parse_date(payload.get("default_effective_date")) if isinstance(payload, dict) else ""

	row_results = []
	cancellations = []
	seen_keys = set()
	for index, raw_row in enumerate(rows, start=1):
		if not isinstance(raw_row, dict):
			row_results.append(_row_error(index, None, "row", _("Row must be an object.")))
			continue
		row = _normalize_enrollment_cancellation_row(raw_row, index, default_effective_date=default_effective_date)
		key = _enrollment_cancellation_import_key(row)
		if key and key in seen_keys:
			row["duplicate_in_file"] = True
		elif key:
			seen_keys.add(key)
		row_results.append(row)
		if not row.get("errors"):
			cancellations.append(row)
	return {
		"rows": row_results,
		"cancellations": cancellations,
		"source_filename": payload.get("source_filename") if isinstance(payload, dict) else "",
	}


def _normalize_enrollment_cancellation_row(raw_row, row_number, default_effective_date=""):
	row = {str(key or "").strip(): _clean_text(value) for key, value in raw_row.items()}
	student_value = _normalize_spaces(_first(row, ["student", "Student", "student_id", "Student ID"]))
	student_name = _normalize_spaces(_first(row, ["student_name", "Student Name", "Name"]))
	normalized = {
		"row_number": row_number,
		"source": row,
		"enrollment": _normalize_spaces(_first(row, ["enrollment", "Enrollment", "Enrollment ID", "enrollment_id", "name"])),
		"student": student_value if student_value and frappe.db.exists("Student", student_value) else "",
		"student_name": student_name or ("" if student_value and frappe.db.exists("Student", student_value) else student_value),
		"student_dob": _parse_date(_first(row, ["student_dob", "Student DOB", "DOB", "Date of Birth", "date_of_birth"])),
		"parent": _normalize_spaces(_first(row, ["parent", "Parent", "parent_id", "Parent ID"])),
		"parent_email": _normalize_email(_first(row, ["parent_email", "Parent Email", "email", "Email", "linked_user"])),
		"invoice": _normalize_spaces(_first(row, ["invoice", "Invoice", "sales_invoice", "Sales Invoice", "invoice_id", "Invoice ID"])),
		"term": _normalize_spaces(_first(row, ["term", "Term"])),
		"course": _normalize_spaces(_first(row, ["course", "Course"])),
		"weekly_timeslot": _normalize_spaces(_first(row, ["weekly_timeslot", "Weekly Timeslot", "timeslot"])),
		"reason": _normalize_spaces(_first(row, ["reason", "Reason"])) or "Batch enrollment cancellation import",
		"effective_date": _parse_date(_first(row, ["effective_date", "Effective Date", "end_date", "End Date"])) or default_effective_date or today(),
		"source_note": _normalize_spaces(_first(row, ["source_note", "Source Note", "note", "notes", "Notes"])),
		"errors": [],
	}
	if not normalized["enrollment"] and not normalized["invoice"] and not (normalized["parent_email"] and (normalized["student"] or normalized["student_name"])):
		normalized["errors"].append(_field_error("enrollment", _("Enrollment, invoice, or parent_email + student is required.")))
	if normalized["parent"] and not frappe.db.exists("Parent", normalized["parent"]):
		normalized["errors"].append(_field_error("parent", _("Parent {0} was not found.").format(normalized["parent"])))
	if normalized["invoice"] and not frappe.db.exists("Sales Invoice", normalized["invoice"]):
		normalized["errors"].append(_field_error("invoice", _("Sales Invoice {0} was not found.").format(normalized["invoice"])))
	return normalized


def _preview_enrollment_cancellation_batch(batch):
	result = _empty_result(dry_run=True)
	valid_rows = batch.get("cancellations") or []
	result["input"] = {
		"row_count": len(batch.get("rows") or []),
		"parent_count": len({_enrollment_parent_key(row) for row in valid_rows if _enrollment_parent_key(row)}),
		"student_count": len(valid_rows),
		"enrollment_count": len(valid_rows),
		"manual_action_count": 0,
	}

	for row in batch.get("rows") or []:
		for error in row.get("errors") or []:
			result["errors"].append({"row": row.get("row_number"), "parent_email": row.get("parent_email"), **error})

	for row in valid_rows:
		preview = _preview_enrollment_cancellation_row(row)
		result["parents"].append(preview)
		_accumulate_counts(result["counts"], preview.get("counts") or {})
		result["errors"].extend(preview.get("errors") or [])
		result["warnings"].extend(preview.get("warnings") or [])

	result["input"]["manual_action_count"] = _manual_action_count(result.get("parents") or [])
	result["blocking_error_count"] = len(result["errors"])
	result["warning_count"] = len(result["warnings"])
	result["manual_action_count"] = result["input"]["manual_action_count"]
	result["ok"] = result["blocking_error_count"] == 0
	return _finalize_result(result)


def _preview_enrollment_cancellation_row(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	if row.get("duplicate_in_file"):
		counts["enrollment_cancellation_duplicates_in_file"] += 1
		warnings.append(_enrollment_cancellation_issue(row, "enrollment", _("Duplicate row in this CSV. The later duplicate will be skipped.")))
		return _enrollment_cancellation_payload(row, counts, warnings=warnings, skipped=True)

	resolved = _resolve_enrollment_cancellation_target(row)
	_accumulate_counts(counts, resolved.get("counts") or {})
	errors.extend(resolved.get("errors") or [])
	warnings.extend(resolved.get("warnings") or [])
	if errors:
		return _enrollment_cancellation_payload(row, counts, resolved=resolved, errors=errors, warnings=warnings)

	doc = resolved.get("doc")
	status = doc.get("status") or ""
	if status == "Cancelled":
		counts["enrollments_already_cancelled"] += 1
		warnings.append(_enrollment_cancellation_issue(row, "status", _("Enrollment is already Cancelled and will be skipped.")))
		return _enrollment_cancellation_payload(row, counts, resolved=resolved, warnings=warnings, skipped=True)
	if status not in ENROLLMENT_CANCELLATION_ALLOWED_STATUSES:
		errors.append(_enrollment_cancellation_issue(row, "status", _("Only Planned or Active enrollments can be cancelled by import. Current status is {0}.").format(status or _("blank"))))
		return _enrollment_cancellation_payload(row, counts, resolved=resolved, errors=errors, warnings=warnings)

	invoice_action = _classify_enrollment_cancellation_invoice(row, doc)
	_accumulate_counts(counts, invoice_action.get("counts") or {})
	warnings.extend(invoice_action.get("warnings") or [])
	counts["enrollments_to_cancel"] += 1
	attendance_count = _count_future_enrollment_attendance(doc.name, row.get("effective_date"))
	if attendance_count:
		counts["attendance_to_cancel"] += attendance_count
	return _enrollment_cancellation_payload(
		row,
		counts,
		resolved=resolved,
		invoice_action=invoice_action,
		warnings=warnings,
		message=_("Enrollment will be cancelled."),
	)


def _run_enrollment_cancellation_row(row):
	preview = _preview_enrollment_cancellation_row(row)
	if preview.get("errors"):
		frappe.throw("; ".join(error.get("message") for error in preview.get("errors") or []))
	if preview.get("skipped"):
		return preview

	doc = frappe.get_doc("Enrollment", preview.get("enrollment"))
	status_before = doc.get("status")
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	attendance_cancelled = _cancel_future_enrollment_attendance_for_import(doc.name, effective_date=row.get("effective_date"))
	reason = row.get("reason") or "Batch enrollment cancellation import"
	_school_admin_add_comment(
		"Enrollment",
		doc.name,
		_("Enrollment cancelled by School Admin import from {0}. Reason: {1}").format(row.get("effective_date"), reason),
	)

	counts = defaultdict(int, preview.get("counts") or {})
	counts["enrollments_to_cancel"] = max(0, counts.get("enrollments_to_cancel", 0) - 1)
	counts["enrollments_cancelled"] += 1
	if attendance_cancelled:
		counts["attendance_to_cancel"] = max(0, counts.get("attendance_to_cancel", 0) - attendance_cancelled)
		counts["attendance_cancelled"] += attendance_cancelled

	invoice_action = preview.get("invoice_action_detail") or {}
	invoice_result = _apply_enrollment_cancellation_invoice_action(invoice_action, reason)
	_mark_invoice_action_completed_in_counts(counts, invoice_action.get("action"))
	if invoice_result.get("count_key"):
		counts[invoice_result.get("count_key")] += 1

	result = {
		**preview,
		"status": "Cancelled",
		"enrollment_status_before": status_before,
		"attendance_cancelled": attendance_cancelled,
		"counts": dict(counts),
		"message": _enrollment_cancellation_run_message(invoice_result, attendance_cancelled),
	}
	if invoice_result:
		result["invoice_action"] = invoice_result.get("action") or result.get("invoice_action")
		result["invoice_message"] = invoice_result.get("message")
		result["manual_action_required"] = invoice_result.get("manual_action_required", result.get("manual_action_required"))
		result["invoice_status"] = invoice_result.get("invoice_status") or result.get("invoice_status")
	return result


def _resolve_enrollment_cancellation_target(row):
	counts = defaultdict(int)
	errors = []
	warnings = []
	doc = None
	match_method = ""

	if row.get("enrollment"):
		if frappe.db.exists("Enrollment", row.get("enrollment")):
			doc = frappe.get_doc("Enrollment", row.get("enrollment"))
			match_method = "enrollment"
		else:
			errors.append(_enrollment_cancellation_issue(row, "enrollment", _("Enrollment {0} was not found.").format(row.get("enrollment"))))
	elif row.get("invoice"):
		resolved = _resolve_cancellation_by_invoice(row)
		doc = resolved.get("doc")
		match_method = resolved.get("match_method") or ""
		errors.extend(resolved.get("errors") or [])
		warnings.extend(resolved.get("warnings") or [])
	else:
		resolved = _resolve_cancellation_by_parent_student(row)
		doc = resolved.get("doc")
		match_method = resolved.get("match_method") or ""
		errors.extend(resolved.get("errors") or [])
		warnings.extend(resolved.get("warnings") or [])

	if doc:
		counts["enrollments_matched"] += 1
		parent = doc.get("parent") or row.get("parent")
		student = doc.get("student") or row.get("student")
		return {
			"doc": doc,
			"parent": parent,
			"student": student,
			"student_name": _student_name(student, row),
			"match_method": match_method,
			"counts": counts,
			"errors": errors,
			"warnings": warnings,
		}
	return {"counts": counts, "errors": errors, "warnings": warnings}


def _resolve_cancellation_by_invoice(row):
	errors = []
	warnings = []
	invoice = row.get("invoice")
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		return {"errors": [_enrollment_cancellation_issue(row, "invoice", _("Sales Invoice {0} was not found.").format(invoice))]}

	student = _resolve_cancellation_student(row)
	enrollment_names = _invoice_enrollment_names(frappe.get_doc("Sales Invoice", invoice))
	if student:
		candidates = _find_enrollments_for_student_invoice(student, invoice, enrollment_names)
		if len(candidates) == 1:
			return {"doc": frappe.get_doc("Enrollment", candidates[0]), "match_method": "invoice_student"}
		if len(candidates) > 1:
			errors.append(_enrollment_cancellation_issue(row, "enrollment", _("Multiple enrollments match this invoice and student. Add the enrollment column."), matches=candidates))
		else:
			errors.append(_enrollment_cancellation_issue(row, "student", _("No enrollment matched this invoice and student.")))
		return {"errors": errors, "warnings": warnings}

	if len(enrollment_names) == 1:
		warnings.append(_enrollment_cancellation_issue(row, "student", _("Matched by the single enrollment linked to the invoice because no student was supplied.")))
		return {"doc": frappe.get_doc("Enrollment", enrollment_names[0]), "match_method": "invoice_single_enrollment", "warnings": warnings}
	errors.append(_enrollment_cancellation_issue(row, "student", _("Student is required when matching by invoice unless the invoice has exactly one linked enrollment."), matches=enrollment_names))
	return {"errors": errors, "warnings": warnings}


def _resolve_cancellation_by_parent_student(row):
	errors = []
	parent = row.get("parent")
	if not parent:
		matches = _find_parent_matches(row.get("parent_email"))
		if len(matches) > 1:
			errors.append(_enrollment_cancellation_issue(row, "parent_email", _("Multiple Parent records match email {0}.").format(row.get("parent_email")), matches=matches))
			return {"errors": errors}
		if matches:
			parent = matches[0]
	if not parent:
		errors.append(_enrollment_cancellation_issue(row, "parent_email", _("Could not match a Parent from {0}.").format(row.get("parent_email"))))
		return {"errors": errors}

	student = _resolve_cancellation_student(row, parent=parent)
	if not student:
		errors.append(_enrollment_cancellation_issue(row, "student", _("Could not match the student under this parent.")))
		return {"errors": errors}

	filters = {
		"student": student,
		"status": ["in", list(ENROLLMENT_CANCELLATION_ALLOWED_STATUSES)],
	}
	if parent and _has_field("Enrollment", "parent"):
		filters["parent"] = parent
	if row.get("term"):
		filters["term"] = row.get("term")
	if row.get("course"):
		filters["course"] = row.get("course")
	if row.get("weekly_timeslot"):
		filters["weekly_timeslot"] = row.get("weekly_timeslot")
	candidates = frappe.get_all("Enrollment", filters=filters, pluck="name", limit_page_length=20)
	if len(candidates) == 1:
		return {"doc": frappe.get_doc("Enrollment", candidates[0]), "match_method": "parent_student"}
	if len(candidates) > 1:
		errors.append(_enrollment_cancellation_issue(row, "enrollment", _("Multiple open enrollments match this parent and student. Add enrollment, term, or weekly_timeslot."), matches=candidates))
	else:
		errors.append(_enrollment_cancellation_issue(row, "enrollment", _("No open Planned or Active enrollment matched this parent and student.")))
	return {"errors": errors}


def _resolve_cancellation_student(row, parent=None):
	if row.get("student") and frappe.db.exists("Student", row.get("student")):
		if parent and not _student_can_belong_to_parent(row.get("student"), parent):
			return None
		return row.get("student")
	if parent and row.get("student_name"):
		matches = _find_student_matches(parent, {"student_name": row.get("student_name"), "student_dob": row.get("student_dob")})
		if len(matches) == 1:
			return matches[0]
	if row.get("student_name") and row.get("student_dob"):
		matches = _find_student_identity_matches({"student_name": row.get("student_name"), "student_dob": row.get("student_dob")})
		if len(matches) == 1:
			return matches[0]
	return None


def _find_enrollments_for_student_invoice(student, invoice, invoice_enrollments=None):
	matches = []
	if invoice_enrollments:
		rows = frappe.get_all(
			"Enrollment",
			filters={"name": ["in", invoice_enrollments], "student": student},
			pluck="name",
			limit_page_length=0,
		)
		matches.extend(rows)
	if _has_field("Enrollment", "invoice"):
		matches.extend(frappe.get_all("Enrollment", filters={"invoice": invoice, "student": student}, pluck="name", limit_page_length=0))
	if _has_field("Sales Invoice", "enrollment"):
		header_enrollment = frappe.db.get_value("Sales Invoice", invoice, "enrollment")
		if header_enrollment:
			matches.extend(frappe.get_all("Enrollment", filters={"name": header_enrollment, "student": student}, pluck="name", limit_page_length=0))
	return _unique(matches)


def _classify_enrollment_cancellation_invoice(row, enrollment_doc):
	counts = defaultdict(int)
	warnings = []
	invoice_name = row.get("invoice") or _existing_invoice_for_enrollment(enrollment_doc)
	if not invoice_name:
		counts["invoices_not_found"] += 1
		return {"action": "none", "counts": counts, "message": _("No linked invoice was found.")}
	if not frappe.db.exists("Sales Invoice", invoice_name):
		counts["invoices_not_found"] += 1
		warnings.append(_enrollment_cancellation_issue(row, "invoice", _("Linked invoice {0} was not found.").format(invoice_name)))
		return {"action": "none", "invoice": invoice_name, "counts": counts, "warnings": warnings}

	doc = frappe.get_doc("Sales Invoice", invoice_name)
	invoice_status = _invoice_status_for_report(doc)
	if cint(doc.docstatus) == 2 or invoice_status == "Cancelled":
		counts["invoices_already_cancelled"] += 1
		return {
			"action": "already_cancelled",
			"invoice": invoice_name,
			"invoice_status": invoice_status,
			"counts": counts,
			"message": _("Linked invoice is already cancelled."),
		}

	safety = _invoice_single_enrollment_safety(doc, enrollment_doc.name)
	if not safety.get("safe"):
		counts["invoices_require_manual_adjustment"] += 1
		message = safety.get("message") or _("Invoice may include other enrollments or students.")
		warnings.append(_enrollment_cancellation_issue(row, "invoice", _("Invoice {0} requires manual adjustment: {1}").format(invoice_name, message)))
		return {
			"action": "manual_adjustment",
			"invoice": invoice_name,
			"invoice_status": invoice_status,
			"counts": counts,
			"warnings": warnings,
			"manual_action_required": True,
			"message": message,
		}

	if cint(doc.docstatus) == 0:
		counts["invoices_to_cancel"] += 1
		return {"action": "cancel_draft", "invoice": invoice_name, "invoice_status": invoice_status, "counts": counts}
	if cint(doc.docstatus) == 1 and payment_mutations_enabled():
		counts["invoices_to_cancel"] += 1
		return {"action": "cancel_submitted", "invoice": invoice_name, "invoice_status": invoice_status, "counts": counts}

	counts["invoices_require_manual_adjustment"] += 1
	message = payment_block_reason() if cint(doc.docstatus) == 1 else _("Invoice docstatus is not supported for automatic cancellation.")
	warnings.append(_enrollment_cancellation_issue(row, "invoice", _("Invoice {0} requires manual adjustment: {1}").format(invoice_name, message)))
	return {
		"action": "manual_adjustment",
		"invoice": invoice_name,
		"invoice_status": invoice_status,
		"counts": counts,
		"warnings": warnings,
		"manual_action_required": True,
		"message": message,
	}


def _apply_enrollment_cancellation_invoice_action(invoice_action, reason):
	action = invoice_action.get("action")
	invoice = invoice_action.get("invoice")
	if not invoice or action in ("none", "already_cancelled"):
		return {
			"action": action or "none",
			"message": invoice_action.get("message"),
			"invoice_status": invoice_action.get("invoice_status"),
			"manual_action_required": False,
		}
	if action == "manual_adjustment":
		return {
			"action": action,
			"message": _("Enrollment cancelled. Invoice requires manual adjustment: {0}").format(invoice_action.get("message") or invoice),
			"invoice_status": invoice_action.get("invoice_status"),
			"manual_action_required": True,
			"count_key": "invoices_manual_adjustment_reported",
		}
	if action == "cancel_draft":
		doc = frappe.get_doc("Sales Invoice", invoice)
		_mark_draft_invoice_cancelled(doc, reason)
		_clear_deleted_invoice_enrollment_snapshot(doc, action="cancelled")
		return {
			"action": action,
			"message": _("Draft invoice was marked Cancelled."),
			"invoice_status": "Cancelled",
			"manual_action_required": False,
			"count_key": "invoices_cancelled",
		}
	if action == "cancel_submitted":
		cancel_school_admin_invoice_data(invoice=invoice, reason=reason)
		return {
			"action": action,
			"message": _("Submitted invoice was cancelled with the existing School Admin invoice cancellation flow."),
			"invoice_status": "Cancelled",
			"manual_action_required": False,
			"count_key": "invoices_cancelled",
		}
	return {
		"action": action,
		"message": _("Invoice action was not recognized. Review manually."),
		"invoice_status": invoice_action.get("invoice_status"),
		"manual_action_required": True,
		"count_key": "invoices_manual_adjustment_reported",
	}


def _invoice_single_enrollment_safety(invoice_doc, enrollment):
	enrollment_names = _invoice_enrollment_names(invoice_doc)
	if enrollment_names:
		unique_enrollments = set(enrollment_names)
		if unique_enrollments == {enrollment}:
			return {"safe": True}
		return {"safe": False, "message": _("Invoice has linked enrollment items: {0}").format(", ".join(sorted(unique_enrollments)))}

	if _has_field("Sales Invoice", "enrollment") and invoice_doc.get("enrollment"):
		return {"safe": invoice_doc.get("enrollment") == enrollment, "message": _("Invoice header links to another enrollment.")}
	if _has_field("Sales Invoice", "source_doctype") and invoice_doc.get("source_doctype") == "Enrollment":
		return {"safe": invoice_doc.get("source_document") == enrollment, "message": _("Invoice source links to another enrollment.")}

	return {"safe": False, "message": _("Invoice does not expose a single enrollment link.")}


def _invoice_enrollment_names(invoice_doc):
	names = []
	for item in invoice_doc.get("items", []) or []:
		enrollment = item.get("enrollment")
		if enrollment:
			names.append(enrollment)
	return _unique(names)


def _count_future_enrollment_attendance(enrollment, effective_date=None):
	if not _doctype_available(ATTENDANCE_DOCTYPE):
		return 0
	filters = {
		"source_doctype": "Enrollment",
		"source_document": enrollment,
		"status": ["in", ["To be started", "Scheduled"]],
	}
	if effective_date:
		session_ids = frappe.get_all(
			"Course Sessions",
			filters={"session_date": [">=", getdate(effective_date)]},
			pluck="name",
			limit_page_length=0,
		)
		if not session_ids:
			return 0
		filters["course_session"] = ["in", session_ids]
	return frappe.db.count(ATTENDANCE_DOCTYPE, filters)


def _cancel_future_enrollment_attendance_for_import(enrollment, effective_date=None):
	if not _doctype_available(ATTENDANCE_DOCTYPE):
		return 0
	filters = {
		"source_doctype": "Enrollment",
		"source_document": enrollment,
		"status": ["in", ["To be started", "Scheduled"]],
	}
	if effective_date:
		session_ids = frappe.get_all(
			"Course Sessions",
			filters={"session_date": [">=", getdate(effective_date)]},
			pluck="name",
			limit_page_length=0,
		)
		if session_ids:
			filters["course_session"] = ["in", session_ids]
		else:
			return 0
	rows = frappe.get_all(ATTENDANCE_DOCTYPE, filters=filters, pluck="name", limit_page_length=0)
	for row in rows:
		frappe.db.set_value(ATTENDANCE_DOCTYPE, row, "status", "Cancelled", update_modified=True)
	return len(rows)


def _enrollment_cancellation_payload(row, counts, resolved=None, invoice_action=None, errors=None, warnings=None, skipped=False, message=None):
	resolved = resolved or {}
	doc = resolved.get("doc")
	invoice_action = invoice_action or {}
	student = resolved.get("student") or (doc.get("student") if doc else row.get("student"))
	parent = resolved.get("parent") or (doc.get("parent") if doc else row.get("parent"))
	invoice = invoice_action.get("invoice") or row.get("invoice") or (doc.get("invoice") if doc else "")
	manual_action = bool(invoice_action.get("manual_action_required"))
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent": parent,
		"student": student,
		"student_name": _student_name(student, row),
		"student_count": 1,
		"enrollment": doc.name if doc else row.get("enrollment"),
		"enrollment_status": doc.get("status") if doc else "",
		"status": "Skipped" if skipped else (doc.get("status") if doc else ""),
		"effective_date": row.get("effective_date"),
		"reason": row.get("reason"),
		"invoice": invoice,
		"invoice_status": invoice_action.get("invoice_status"),
		"invoice_action": _invoice_action_label(invoice_action.get("action")),
		"invoice_action_detail": invoice_action,
		"manual_action_required": manual_action,
		"message": message or invoice_action.get("message") or "",
		"skipped": skipped,
		"counts": dict(counts),
		"errors": errors or [],
		"warnings": warnings or [],
		"raw_row": row.get("source"),
	}


def _enrollment_cancellation_error_payload(row, exc):
	counts = defaultdict(int)
	counts["enrollment_cancellation_errors"] += 1
	return {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"parent_record": row.get("parent"),
		"student": row.get("student"),
		"student_name": row.get("student_name"),
		"enrollment": row.get("enrollment"),
		"invoice": row.get("invoice"),
		"status": "Error",
		"message": str(exc),
		"manual_action_required": False,
		"counts": dict(counts),
		"errors": [_enrollment_cancellation_issue(row, "enrollment_cancellation", str(exc))],
		"warnings": [],
		"raw_row": row.get("source"),
	}


def _enrollment_cancellation_run_message(invoice_result, attendance_cancelled):
	pieces = [_("Enrollment cancelled.")]
	if attendance_cancelled:
		pieces.append(_("{0} future attendance rows cancelled.").format(attendance_cancelled))
	if invoice_result.get("message"):
		pieces.append(invoice_result.get("message"))
	return " ".join(str(piece) for piece in pieces if piece)


def _enrollment_cancellation_issue(row, field, message, matches=None):
	issue = {
		"row": row.get("row_number"),
		"parent_email": row.get("parent_email"),
		"field": field,
		"message": str(message),
	}
	if matches:
		issue["matches"] = matches
	return issue


def _enrollment_cancellation_import_key(row):
	return "|".join(
		_normalized_key(part)
		for part in [
			row.get("enrollment"),
			row.get("invoice"),
			row.get("parent") or row.get("parent_email"),
			row.get("student") or row.get("student_name"),
			row.get("term"),
			row.get("weekly_timeslot"),
		]
	)


def _student_name(student, row=None):
	if student and frappe.db.exists("Student", student):
		if _has_field("Student", "student_name"):
			return frappe.db.get_value("Student", student, "student_name") or student
		return student
	return (row or {}).get("student_name") or student or ""


def _invoice_status_for_report(doc):
	if doc.get("status"):
		return doc.get("status")
	if cint(doc.docstatus) == 2:
		return "Cancelled"
	if cint(doc.docstatus) == 1:
		return "Submitted"
	return "Draft"


def _invoice_action_label(action):
	return {
		"none": "No invoice",
		"already_cancelled": "Already cancelled",
		"manual_adjustment": "Manual adjustment required",
		"cancel_draft": "Cancel draft invoice",
		"cancel_submitted": "Cancel submitted invoice",
	}.get(action or "", action or "")


def _manual_action_count(rows):
	return sum(1 for row in rows or [] if row.get("manual_action_required"))


def _save_operation_report(
	report_type,
	status,
	result,
	started_at=None,
	source=None,
	source_reference=None,
	source_filename=None,
	success_count=None,
):
	if not _doctype_available(OPERATION_REPORT_DOCTYPE):
		return None
	finished_at = now_datetime()
	rows = result.get("parents") or []
	doc = frappe.new_doc(OPERATION_REPORT_DOCTYPE)
	doc.report_type = report_type
	doc.source = source or ""
	doc.source_reference = source_reference or ""
	doc.status = status
	doc.source_filename = source_filename or ""
	doc.started_at = started_at
	doc.finished_at = finished_at
	doc.input_row_count = (result.get("input") or {}).get("row_count") or 0
	doc.success_count = success_count if success_count is not None else ((result.get("counts") or {}).get("enrollments_cancelled") or 0)
	doc.error_count = result.get("blocking_error_count") or result.get("error_count") or len(result.get("errors") or [])
	doc.warning_count = result.get("warning_count") or len(result.get("warnings") or [])
	doc.manual_action_count = result.get("manual_action_count") or _manual_action_count(rows)
	for row in rows[:500]:
		doc.append("rows", _operation_report_child_row(row, blocked=status == "Blocked"))
	doc.insert(ignore_permissions=True)
	doc.db_set(
		"report_json",
		json.dumps(
			_report_json_payload(
				result,
				doc.name,
				status,
				finished_at,
				report_type=report_type,
				source=source,
				source_reference=source_reference,
			),
			default=str,
			sort_keys=True,
		),
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"name": doc.name,
		"report_type": doc.report_type,
		"import_type": doc.report_type,
		"source": doc.source,
		"source_reference": doc.source_reference,
		"status": doc.status,
		"finished_at": doc.finished_at,
		"input_row_count": doc.input_row_count,
		"success_count": doc.success_count,
		"error_count": doc.error_count,
		"warning_count": doc.warning_count,
		"manual_action_count": doc.manual_action_count,
	}


def _report_json_payload(result, report_name, status, finished_at, report_type=None, source=None, source_reference=None):
	payload = dict(result or {})
	payload["report_name"] = report_name
	payload["report_type"] = report_type
	payload["report_source"] = source
	payload["report_source_reference"] = source_reference
	payload["report_status"] = status
	payload["report_finished_at"] = str(finished_at)
	return payload


def _operation_report_child_row(row, blocked=False):
	return {
		"row_number": row.get("row"),
		"row_status": _operation_report_row_status(row, blocked=blocked),
		"action": _operation_report_row_action(row),
		"manual_action_required": 1 if row.get("manual_action_required") else 0,
		"reference_doctype": _operation_report_reference_doctype(row),
		"reference_name": _operation_report_reference_name(row),
		"parent_record": row.get("parent") or row.get("parent_record"),
		"parent_email": row.get("parent_email"),
		"student": row.get("student"),
		"student_name": row.get("student_name"),
		"enrollment": row.get("enrollment"),
		"invoice": row.get("invoice"),
		"invoice_action": row.get("invoice_action"),
		"message": row.get("message") or row.get("invoice_message") or "",
		"raw_row_json": json.dumps(row.get("raw_row") or {}, default=str, sort_keys=True),
	}


def _operation_report_child_row_payload(row):
	return {
		"row": row.get("row_number"),
		"status": row.get("row_status"),
		"action": row.get("action"),
		"manual_action_required": bool(row.get("manual_action_required")),
		"reference_doctype": row.get("reference_doctype"),
		"reference_name": row.get("reference_name"),
		"parent": row.get("parent_record"),
		"parent_email": row.get("parent_email"),
		"student": row.get("student"),
		"student_name": row.get("student_name"),
		"enrollment": row.get("enrollment"),
		"invoice": row.get("invoice"),
		"invoice_action": row.get("invoice_action"),
		"message": row.get("message"),
		"raw_row": _decode_report_json(row.get("raw_row_json")) or {},
	}


def _operation_report_row_status(row, blocked=False):
	if row.get("operation_status"):
		return row.get("operation_status")
	if row.get("errors"):
		return "Error"
	if blocked or row.get("skipped"):
		return "Skipped"
	return "Cancelled"


def _operation_report_row_action(row):
	if row.get("operation_action"):
		return row.get("operation_action")
	if row.get("invoice_action"):
		return row.get("invoice_action")
	if row.get("status"):
		return row.get("status")
	return ""


def _operation_report_reference_doctype(row):
	if row.get("enrollment"):
		return "Enrollment"
	if row.get("invoice"):
		return "Sales Invoice"
	if row.get("student"):
		return "Student"
	if row.get("parent"):
		return "Parent"
	return ""


def _operation_report_reference_name(row):
	return row.get("enrollment") or row.get("invoice") or row.get("student") or row.get("parent") or ""


def _decode_report_json(value):
	if not value:
		return None
	try:
		return json.loads(value)
	except Exception:
		return None


def _report_type_label(value):
	if value in ("enrollment_cancellation", ENROLLMENT_CANCELLATION_IMPORT_TYPE):
		return ENROLLMENT_CANCELLATION_IMPORT_TYPE
	if value in ("enrollment_change", ENROLLMENT_CHANGE_REPORT_TYPE):
		return ENROLLMENT_CHANGE_REPORT_TYPE
	if value in ("invoice_enrollment_reset", INVOICE_ENROLLMENT_RESET_REPORT_TYPE):
		return INVOICE_ENROLLMENT_RESET_REPORT_TYPE
	return value


def _limit(value, default=20, max_value=100):
	value = cint(value or default)
	if value <= 0:
		value = default
	return min(value, max_value)


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
