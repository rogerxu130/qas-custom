"""Background generation of term attendance and draft invoices."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime


OPERATIONS = {"attendance", "invoices"}
ACTIVE_STATUSES = {"queued", "running"}
JOB_TTL_SECONDS = 172800
JOB_STALE_SECONDS = 10800


def _status_key(term, operation):
	return f"qas:school_admin:term_generation:{term}:{operation}:status"


def _get_status(term, operation):
	status = frappe.cache().get_value(_status_key(term, operation), expires=True)
	if status and status.get("status") in ACTIVE_STATUSES:
		updated = get_datetime(status.get("updated_at") or status.get("created_at"))
		if updated and (now_datetime() - updated).total_seconds() > JOB_STALE_SECONDS:
			status["status"] = "failed"
			status["completed_at"] = now_datetime().isoformat()
			status["error_rows"].append({"enrollment": status.get("current_enrollment"), "error": _("The background job stopped updating. You can start it again; completed records will be skipped.")})
			_set_status(status)
	return status


def _set_status(status):
	status["updated_at"] = now_datetime().isoformat()
	frappe.cache().set_value(_status_key(status["term"], status["operation"]), status, expires_in_sec=JOB_TTL_SECONDS)


def _validate_operation(operation):
	operation = (operation or "").strip().lower()
	if operation not in OPERATIONS:
		frappe.throw(_("Operation must be attendance or invoices."))
	return operation


def get_school_admin_term_generation_job_data(term=None, operation=None):
	from qas_custom.services import school_admin

	school_admin._require_school_admin()
	operation = _validate_operation(operation)
	if not term or not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))
	return _get_status(term, operation)


def start_school_admin_term_generation_job_data(term=None, operation=None):
	from qas_custom.services import school_admin

	school_admin._require_school_admin()
	operation = _validate_operation(operation)
	if not term or not frappe.db.exists("Term", term):
		frappe.throw(_("Term was not found."))

	cache = frappe.cache()
	lock_name = f"{frappe.local.site}:qas:term_generation:{term}"
	with cache.lock(lock_name, timeout=30, blocking_timeout=10):
		for kind in OPERATIONS:
			current = _get_status(term, kind)
			if current and current.get("status") in ACTIVE_STATUSES:
				if kind == operation:
					return current
				frappe.throw(_("Another term generation job is already running. Wait for it to finish."))

		if operation == "attendance":
			names = school_admin._get_attendance_candidate_enrollment_names(term=term)
		else:
			names = school_admin._get_invoice_candidate_enrollment_names(term=term)
		status = {
			"job_id": frappe.generate_hash(length=16),
			"term": term,
			"operation": operation,
			"status": "queued",
			"total": len(names),
			"processed": 0,
			"succeeded": 0,
			"skipped": 0,
			"failed": 0,
			"activated_enrollments": 0,
			"attendance_entries": 0,
			"created_invoices": 0,
			"invoice_items": 0,
			"current_enrollment": None,
			"error_rows": [],
			"warnings": [],
			"created_at": now_datetime().isoformat(),
			"started_at": None,
			"completed_at": None,
		}
		_set_status(status)
		try:
			frappe.enqueue(
				"qas_custom.services.term_generation_jobs.run_school_admin_term_generation_job",
				queue="long",
				timeout=7200,
				job_id=f"qas-term-generation-{status['job_id']}",
				enqueue_after_commit=True,
				qas_job_id=status["job_id"],
				term=term,
				operation=operation,
				enrollment_names=names,
				requested_by=frappe.session.user,
			)
		except Exception:
			status["status"] = "failed"
			status["completed_at"] = now_datetime().isoformat()
			_set_status(status)
			raise
	return status


def run_school_admin_term_generation_job(qas_job_id=None, term=None, operation=None, enrollment_names=None, requested_by=None):
	from qas_custom.services import school_admin

	operation = _validate_operation(operation)
	status = _get_status(term, operation)
	if not status or status.get("job_id") != qas_job_id:
		return
	if requested_by:
		frappe.set_user(requested_by)
	status.update({"status": "running", "started_at": now_datetime().isoformat()})
	_set_status(status)
	try:
		for enrollment in enrollment_names or []:
			latest = _get_status(term, operation)
			if not latest or latest.get("job_id") != qas_job_id:
				return
			status["current_enrollment"] = enrollment
			_set_status(status)
			try:
				if operation == "attendance":
					result = school_admin._create_attendance_for_enrollment_names([enrollment])
				else:
					result = school_admin._create_invoices_for_enrollment_names([enrollment])
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback()
				result = {"errors": 1, "error_rows": [{"enrollment": enrollment, "error": school_admin._bulk_action_error_message(exc)}]}
			status["processed"] += 1
			status["skipped"] += result.get("skipped", 0)
			status["failed"] += result.get("errors", 0)
			status["succeeded"] += int(not result.get("skipped") and not result.get("errors"))
			for field in ("activated_enrollments", "attendance_entries", "created_invoices", "invoice_items"):
				status[field] += result.get(field, 0)
			status["error_rows"].extend(result.get("error_rows") or [])
			status["warnings"].extend(result.get("warnings") or [])
			_set_status(status)
		status["status"] = "completed_with_errors" if status["failed"] else "completed"
	except Exception as exc:
		frappe.db.rollback()
		status["status"] = "failed"
		status["error_rows"].append({"enrollment": status.get("current_enrollment"), "error": school_admin._bulk_action_error_message(exc)})
	finally:
		status["current_enrollment"] = None
		status["completed_at"] = now_datetime().isoformat()
		latest = _get_status(term, operation)
		if latest and latest.get("job_id") == qas_job_id:
			_set_status(status)
	return status
