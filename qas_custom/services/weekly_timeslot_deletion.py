"""Reviewed deletion of an empty weekly class and its unstarted sessions."""
import hashlib
import json

import frappe
from frappe import _
from frappe.model.delete_doc import get_linked_docs, get_dynamic_linked_docs
from frappe.utils import now_datetime

from qas_custom.services.school_admin import _require_school_admin
from qas_custom.services.weekly_timeslot_course_change import _session_has_started


def _report(weekly_timeslot):
	if not weekly_timeslot:
		frappe.throw(_("Weekly timeslot is required."))
	doc = frappe.get_doc("Weekly Timeslot", weekly_timeslot)
	sessions = frappe.get_all("Course Sessions", filters={"weekly_timeslot": doc.name},
		fields=["name", "session_date", "status", "modified"], order_by="name asc", limit_page_length=0)
	errors = []
	current_time = now_datetime()
	for row in sessions:
		if not row.get("session_date") or _session_has_started(row, doc.start_time, current_time):
			errors.append(_("Session {0} has started or has no valid date; historical sessions cannot be deleted.").format(row["name"]))
		elif row["status"] not in ("Scheduled", "Cancelled"):
			errors.append(_("Session {0} has status {1} and cannot be deleted.").format(row["name"], row["status"]))
	# Include ALL statuses: a zero active-student count is not enough to delete history.
	for row in frappe.get_all("Enrollment", filters={"weekly_timeslot": doc.name}, pluck="name", limit_page_length=0):
		errors.append(_("Linked Enrollment: {0}").format(row))
	names = [row["name"] for row in sessions]
	if names:
		for dt in ("Inquiry", "Class Attendance Entry"):
			for name in frappe.get_all(dt, filters={"course_session": ["in", names]}, pluck="name", limit_page_length=0):
				errors.append(_("Linked {0}: {1}").format(dt, name))
	# Framework link discovery also covers invoices, makeup, homework, and custom links.
	for linked_doc in [doc] + [frappe.get_doc("Course Sessions", name) for name in names]:
		for link in get_linked_docs(linked_doc) + get_dynamic_linked_docs(linked_doc):
			if linked_doc.doctype == "Weekly Timeslot" and link["reference_doctype"] == "Course Sessions" and link["reference_docname"] in names:
				continue
			errors.append(_("Linked {0}: {1}").format(link["reference_doctype"], link["reference_docname"]))
	report = dict(weekly_timeslot=doc.name, label=doc.get("display_label") or doc.name,
		modified=str(doc.modified), sessions=sessions, session_count=len(sessions),
		blocking_errors=sorted(set(errors)))
	report["confirmation_token"] = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()
	return report


@frappe.whitelist()
def preview(weekly_timeslot=None):
	_require_school_admin()
	return _report(weekly_timeslot)


@frappe.whitelist(methods=["POST"])
def execute(weekly_timeslot=None, confirmation_token=None):
	_require_school_admin()
	if not weekly_timeslot or not confirmation_token:
		frappe.throw(_("Preview and confirm the deletion first."))
	try:
		frappe.db.get_value("Weekly Timeslot", weekly_timeslot, "name", for_update=True)
		for name in frappe.get_all("Course Sessions", filters={"weekly_timeslot": weekly_timeslot},
			pluck="name", order_by="name asc", limit_page_length=0):
			frappe.db.get_value("Course Sessions", name, "name", for_update=True)
		report = _report(weekly_timeslot)
		if report["blocking_errors"]:
			frappe.throw("\n".join(report["blocking_errors"]))
		if report["confirmation_token"] != confirmation_token:
			frappe.throw(_("The class changed after preview. Review the deletion again."))
		# Never force-delete or bypass link checks. Any failure rolls back the whole batch.
		for row in report["sessions"]:
			frappe.delete_doc("Course Sessions", row["name"], ignore_permissions=True, ignore_missing=False)
		frappe.delete_doc("Weekly Timeslot", weekly_timeslot, ignore_permissions=True, ignore_missing=False)
		return dict(weekly_timeslot=weekly_timeslot, deleted_session_count=report["session_count"])
	except Exception:
		frappe.db.rollback()
		raise
