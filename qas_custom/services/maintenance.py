from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, today

from qas_custom.utils.environment import sendmail_or_skip


ACTIVE_INQUIRY_STATUSES = ("New", "Needs Review", "Booked", "Rescheduled", "Completed", "No-show", "Follow-up")
ACTIVE_ADHOC_BOOKING_STATUSES = ("Reserved", "Locked")
INACTIVE_STUDENT_STATUS = "Inactive"
ACTIVE_STUDENT_STATUS = "Active"
ISSUE_DOCTYPE = "QAS Data Issue"


def run_nightly_maintenance():
	student_result = sync_student_activity_status()
	attendance_result = reconcile_attendance_links()
	return {
		"student_activity": student_result,
		"attendance_links": attendance_result,
	}


def sync_student_activity_status():
	if not _doctype_available("Student") or not _has_field("Student", "status"):
		return {"skipped": True, "reason": "Student.status is not available."}
	if not _student_status_supports_active_inactive():
		return {"skipped": True, "reason": "Student.status does not support Active/Inactive."}

	fields = ["name", "status"]
	students = frappe.get_all("Student", fields=fields, limit_page_length=0)
	active_students = _get_students_with_active_business()
	activated = []
	inactivated = []

	for row in students:
		student = row.name
		status = row.get("status")
		should_be_active = student in active_students
		if should_be_active and status != ACTIVE_STUDENT_STATUS:
			frappe.db.set_value("Student", student, "status", ACTIVE_STUDENT_STATUS, update_modified=True)
			activated.append(student)
		elif not should_be_active and status == ACTIVE_STUDENT_STATUS:
			frappe.db.set_value("Student", student, "status", INACTIVE_STUDENT_STATUS, update_modified=True)
			inactivated.append(student)

	frappe.db.commit()
	return {
		"checked": len(students),
		"active_reference_students": len(active_students),
		"activated": activated,
		"inactivated": inactivated,
	}


def reconcile_attendance_links():
	if not _doctype_available(ISSUE_DOCTYPE):
		return {"skipped": True, "reason": f"{ISSUE_DOCTYPE} is not available."}
	if not _doctype_available("Class Attendance Entry"):
		return {"skipped": True, "reason": "Class Attendance Entry is not available."}

	new_issue_names = []
	seen_keys = set()
	for issue in _find_attendance_link_issues():
		issue_name, created = _upsert_data_issue(issue)
		seen_keys.add(issue["issue_key"])
		if created:
			new_issue_names.append(issue_name)

	frappe.db.commit()
	if new_issue_names:
		_notify_school_admins_of_new_issues(new_issue_names)

	return {
		"checked_at": now_datetime(),
		"issues_seen": len(seen_keys),
		"new_issues": new_issue_names,
	}


def _get_students_with_active_business():
	students = set()
	students.update(_pluck_students("Enrollment", {"status": "Active"}))
	students.update(_pluck_students("Inquiry", {"status": ["in", ACTIVE_INQUIRY_STATUSES]}))
	students.update(_pluck_students("Adhoc Booking", {"status": ["in", ACTIVE_ADHOC_BOOKING_STATUSES]}))
	students.update(_get_students_with_future_attendance())
	students.update(_get_students_with_open_course_invoices())
	return {student for student in students if student}


def _get_students_with_future_attendance():
	if not _doctype_available("Class Attendance Entry") or not _doctype_available("Course Sessions"):
		return set()
	sessions = frappe.get_all(
		"Course Sessions",
		filters=_course_session_active_filters(from_today=True),
		pluck="name",
		limit_page_length=0,
	)
	if not sessions:
		return set()
	return set(
		frappe.get_all(
			"Class Attendance Entry",
			filters={"course_session": ["in", sessions], "status": ["!=", "Cancelled"]},
			pluck="student",
			limit_page_length=0,
		)
	)


def _get_students_with_open_course_invoices():
	if not _doctype_available("Sales Invoice") or not _has_field("Sales Invoice", "student"):
		return set()
	filters = {"docstatus": ["!=", 2]}
	if _has_field("Sales Invoice", "status"):
		filters["status"] = ["not in", ["Paid", "Cancelled", "Return", "Credit Note Issued"]]
	return set(frappe.get_all("Sales Invoice", filters=filters, pluck="student", limit_page_length=0))


def _find_attendance_link_issues():
	yield from _find_orphan_attendance_source_issues()
	yield from _find_duplicate_attendance_issues()
	yield from _find_inquiry_attendance_issues()
	yield from _find_adhoc_attendance_issues()


def _find_orphan_attendance_source_issues():
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters={"source_doctype": ["is", "set"]},
		fields=["name", "source_doctype", "source_document", "student", "course_session", "enrollment_type"],
		limit_page_length=0,
	)
	for row in rows:
		if not row.source_document:
			yield _issue(
				key_parts=["attendance-missing-source-document", row.name],
				severity="Critical",
				source_doctype="Class Attendance Entry",
				source_document=row.name,
				student=row.student,
				course_session=row.course_session,
				description=_("Attendance entry has a source doctype but no source document."),
				suggested_action=_("Review the attendance entry and either link it to the correct source document or remove the source doctype."),
			)
			continue
		if not _doctype_available(row.source_doctype) or not frappe.db.exists(row.source_doctype, row.source_document):
			yield _issue(
				key_parts=["attendance-source-not-found", row.name, row.source_doctype, row.source_document],
				severity="Critical",
				source_doctype="Class Attendance Entry",
				source_document=row.name,
				related_doctype=row.source_doctype,
				related_document=row.source_document,
				student=row.student,
				course_session=row.course_session,
				description=_("Attendance entry points to a source document that no longer exists."),
				suggested_action=_("Review the attendance entry and recreate the source record, relink it, or remove the attendance entry."),
			)


def _find_duplicate_attendance_issues():
	rows = frappe.get_all(
		"Class Attendance Entry",
		fields=["name", "course_session", "student", "enrollment_type", "source_doctype", "source_document"],
		limit_page_length=0,
	)
	grouped = defaultdict(list)
	for row in rows:
		key = (
			row.course_session,
			row.student,
			row.enrollment_type,
			row.source_doctype or "",
			row.source_document or "",
		)
		grouped[key].append(row.name)

	for (course_session, student, enrollment_type, source_doctype, source_document), names in grouped.items():
		if len(names) <= 1:
			continue
		yield _issue(
			key_parts=["duplicate-attendance", course_session, student, enrollment_type, source_doctype, source_document],
			severity="Warning",
			source_doctype=source_doctype or "Class Attendance Entry",
			source_document=source_document or names[0],
			related_doctype="Class Attendance Entry",
			related_document=names[0],
			student=student,
			course_session=course_session,
			description=_("Duplicate attendance entries exist for the same student, session, type and source: {0}.").format(
				", ".join(names)
			),
			suggested_action=_("Review the duplicate attendance entries and remove or merge the extra rows."),
		)


def _find_inquiry_attendance_issues():
	if not _doctype_available("Inquiry"):
		return

	inquiries = frappe.get_all(
		"Inquiry",
		filters={"inquiry_type": "Trial Lesson"},
		fields=["name", "status", "student", "course_session"],
		limit_page_length=0,
	)
	attendance_by_source = _get_attendance_by_source("Inquiry")
	for inquiry in inquiries:
		entries = attendance_by_source.get(inquiry.name, [])
		entries_to_check = entries
		if inquiry.status in {"Booked", "Rescheduled"} and inquiry.course_session and not entries:
			yield _issue(
				key_parts=["inquiry-booked-missing-attendance", inquiry.name],
				severity="Warning",
				source_doctype="Inquiry",
				source_document=inquiry.name,
				student=inquiry.student,
				course_session=inquiry.course_session,
				description=_("Booked trial inquiry has no linked Class Attendance Entry."),
				suggested_action=_("Open and save the Inquiry, or manually recreate the trial attendance entry for the original course session."),
			)
		if inquiry.status == "Cancelled":
			entries_to_check = [entry for entry in entries if entry.status != "Cancelled"]
			if entries_to_check:
				yield _issue(
					key_parts=["inquiry-cancelled-has-attendance", inquiry.name],
					severity="Warning",
					source_doctype="Inquiry",
					source_document=inquiry.name,
					related_doctype="Class Attendance Entry",
					related_document=entries_to_check[0].name,
					student=inquiry.student,
					course_session=entries_to_check[0].course_session,
					description=_("Cancelled trial inquiry still has linked non-cancelled Class Attendance Entry rows."),
					suggested_action=_("Mark the linked trial attendance entry as Cancelled, or reopen the Inquiry if it was cancelled by mistake."),
				)
		for entry in entries_to_check:
			if inquiry.course_session and entry.course_session != inquiry.course_session:
				yield _issue(
					key_parts=["inquiry-attendance-session-mismatch", inquiry.name, entry.name],
					severity="Warning",
					source_doctype="Inquiry",
					source_document=inquiry.name,
					related_doctype="Class Attendance Entry",
					related_document=entry.name,
					student=inquiry.student or entry.student,
					course_session=entry.course_session,
					description=_("Trial inquiry course session does not match its linked attendance entry."),
					suggested_action=_("Review the Inquiry course session and the attendance entry, then keep only the correct session link."),
				)


def _find_adhoc_attendance_issues():
	if not _doctype_available("Adhoc Booking"):
		return

	bookings = frappe.get_all(
		"Adhoc Booking",
		fields=["name", "status", "student", "course_session"],
		limit_page_length=0,
	)
	attendance_by_source = _get_attendance_by_source("Adhoc Booking")
	for booking in bookings:
		entries = attendance_by_source.get(booking.name, [])
		if booking.status in ACTIVE_ADHOC_BOOKING_STATUSES and booking.course_session and not entries:
			yield _issue(
				key_parts=["adhoc-active-missing-attendance", booking.name],
				severity="Warning",
				source_doctype="Adhoc Booking",
				source_document=booking.name,
				student=booking.student,
				course_session=booking.course_session,
				description=_("Active adhoc booking has no linked Class Attendance Entry."),
				suggested_action=_("Review the booking and recreate the Pay-as-you-go attendance entry if the booking is still valid."),
			)
		if booking.status == "Cancelled" and entries:
			yield _issue(
				key_parts=["adhoc-cancelled-has-attendance", booking.name],
				severity="Warning",
				source_doctype="Adhoc Booking",
				source_document=booking.name,
				related_doctype="Class Attendance Entry",
				related_document=entries[0].name,
				student=booking.student,
				course_session=entries[0].course_session,
				description=_("Cancelled adhoc booking still has linked Class Attendance Entry rows."),
				suggested_action=_("Remove the Pay-as-you-go attendance entry if the booking cancellation is final."),
			)
		for entry in entries:
			if booking.course_session and entry.course_session != booking.course_session:
				yield _issue(
					key_parts=["adhoc-attendance-session-mismatch", booking.name, entry.name],
					severity="Warning",
					source_doctype="Adhoc Booking",
					source_document=booking.name,
					related_doctype="Class Attendance Entry",
					related_document=entry.name,
					student=booking.student or entry.student,
					course_session=entry.course_session,
					description=_("Adhoc booking course session does not match its linked attendance entry."),
					suggested_action=_("Review the booking course session and the attendance entry, then keep only the correct session link."),
				)


def _get_attendance_by_source(source_doctype):
	rows = frappe.get_all(
		"Class Attendance Entry",
		filters={"source_doctype": source_doctype, "source_document": ["is", "set"]},
		fields=["name", "source_document", "student", "course_session", "status"],
		limit_page_length=0,
	)
	grouped = defaultdict(list)
	for row in rows:
		grouped[row.source_document].append(row)
	return grouped


def _issue(
	key_parts,
	severity,
	description,
	suggested_action,
	source_doctype=None,
	source_document=None,
	related_doctype=None,
	related_document=None,
	student=None,
	course_session=None,
):
	return {
		"issue_key": _make_issue_key(key_parts),
		"issue_type": "Attendance Link Mismatch",
		"severity": severity,
		"source_doctype": source_doctype,
		"source_document": source_document,
		"related_doctype": related_doctype,
		"related_document": related_document,
		"student": student,
		"course_session": course_session,
		"description": description,
		"suggested_action": suggested_action,
	}


def _make_issue_key(key_parts):
	raw_key = "|".join(str(part or "-") for part in key_parts)
	prefix = str(key_parts[0] or "issue")[:40]
	return f"{prefix}:{hashlib.sha1(raw_key.encode('utf-8')).hexdigest()[:16]}"


def _upsert_data_issue(issue):
	now = now_datetime()
	existing = frappe.db.get_value(ISSUE_DOCTYPE, {"issue_key": issue["issue_key"]}, "name")
	if existing:
		doc = frappe.get_doc(ISSUE_DOCTYPE, existing)
		created = False
	else:
		doc = frappe.new_doc(ISSUE_DOCTYPE)
		doc.issue_key = issue["issue_key"]
		doc.first_detected_at = now
		doc.occurrence_count = 0
		created = True

	doc.issue_type = issue["issue_type"]
	doc.severity = issue["severity"]
	doc.status = "Open"
	doc.last_seen_at = now
	doc.occurrence_count = (doc.occurrence_count or 0) + 1
	doc.source_doctype = issue.get("source_doctype")
	doc.source_document = issue.get("source_document")
	doc.related_doctype = issue.get("related_doctype")
	doc.related_document = issue.get("related_document")
	doc.student = issue.get("student")
	doc.course_session = issue.get("course_session")
	doc.description = issue["description"]
	doc.suggested_action = issue["suggested_action"]
	if created:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return doc.name, created


def _notify_school_admins_of_new_issues(issue_names):
	recipients = _get_school_admin_emails()
	if not recipients:
		return
	severity_counts = Counter(
		frappe.get_all(
			ISSUE_DOCTYPE,
			filters={"name": ["in", issue_names]},
			pluck="severity",
		)
	)
	message_lines = [
		_("QAS nightly data check found {0} new attendance link issue(s).").format(len(issue_names)),
		"",
		_("Critical: {0}").format(severity_counts.get("Critical", 0)),
		_("Warning: {0}").format(severity_counts.get("Warning", 0)),
		_("Info: {0}").format(severity_counts.get("Info", 0)),
		"",
		_("Please review QAS Data Issue in the backend."),
	]
	try:
		sendmail_or_skip(
			action="nightly_data_issue_alert",
			recipients=recipients,
			subject=_("QAS nightly data issues detected"),
			message="<br>".join(message_lines),
			now=False,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS Data Issue notification failed")


def _get_school_admin_emails():
	if not _doctype_available("Has Role"):
		return []
	users = frappe.get_all(
		"Has Role",
		filters={"role": "School Admin", "parenttype": "User"},
		pluck="parent",
		limit_page_length=0,
	)
	recipients = []
	for user in users:
		if user and user != "Guest" and frappe.db.get_value("User", user, "enabled"):
			email = frappe.db.get_value("User", user, "email")
			if email:
				recipients.append(email)
	return sorted(set(recipients))


def _pluck_students(doctype, filters):
	if not _doctype_available(doctype) or not _has_field(doctype, "student"):
		return set()
	return set(frappe.get_all(doctype, filters=filters, pluck="student", limit_page_length=0))


def _course_session_active_filters(from_today=False):
	filters = {}
	if from_today and _has_field("Course Sessions", "session_date"):
		filters["session_date"] = [">=", getdate(today())]
	if _has_field("Course Sessions", "status"):
		filters["status"] = ["!=", "Cancelled"]
	return filters


def _student_status_supports_active_inactive():
	try:
		field = frappe.get_meta("Student").get_field("status")
	except Exception:
		return False
	options = [option.strip() for option in (field.options or "").splitlines() if option.strip()]
	return ACTIVE_STUDENT_STATUS in options and INACTIVE_STUDENT_STATUS in options


def _doctype_available(doctype):
	try:
		return frappe.db.table_exists(doctype)
	except Exception:
		return False


def _has_field(doctype, fieldname):
	try:
		return frappe.db.has_column(doctype, fieldname)
	except Exception:
		return False
