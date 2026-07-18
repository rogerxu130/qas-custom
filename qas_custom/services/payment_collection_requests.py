from __future__ import annotations

import json
from html import escape

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from qas_custom.modules.billing.store_credit import get_invoice_payable_amount
from qas_custom.services.support_view import reject_support_view_write
from qas_custom.utils.environment import sendmail_or_skip


REQUEST_DOCTYPE = "Payment Collection Request"
REQUEST_TYPES = {"Invoice Payment", "Store Credit Top-up"}
PAYMENT_METHODS = {"Cash", "EFTPOS", "Bank Transfer", "Other"}
RESOLUTION_STATUSES = {"Processed", "Rejected"}


def search_campus_payment_targets_data(query=None, campus=None, limit=40):
	profile, campuses = _campus_scope(campus)
	parent_ids = _campus_parent_ids(campuses)
	query = (query or "").strip().lower()
	limit = min(max(cint(limit or 40), 1), 100)
	parents = _parent_rows(parent_ids)
	students = _students_by_parent(parent_ids)
	invoices = _submitted_invoice_rows(parent_ids, parents)

	family_items = []
	for parent in parents:
		student_labels = students.get(parent.get("name"), [])
		haystack = " ".join(str(value or "") for value in [
			parent.get("name"), parent.get("parent_name"), parent.get("mobile_number"),
			parent.get("phone"), parent.get("email"), parent.get("email_id"), *student_labels,
		]).lower()
		if query and query not in haystack:
			continue
		family_items.append({
			"parent": parent.get("name"),
			"parent_name": parent.get("parent_name") or parent.get("name"),
			"customer": parent.get("customer"),
			"phone": parent.get("mobile_number") or parent.get("phone") or "",
			"email": parent.get("email") or parent.get("email_id") or "",
			"students": student_labels,
		})

	invoice_items = []
	for invoice in invoices:
		parent_name = next((row.get("parent_name") or row.get("name") for row in parents if row.get("name") == invoice.get("parent")), invoice.get("parent"))
		student_labels = students.get(invoice.get("parent"), [])
		haystack = " ".join(str(value or "") for value in [
			invoice.get("name"), invoice.get("customer"), invoice.get("customer_name"),
			invoice.get("parent"), parent_name, invoice.get("student_summary"), *student_labels,
		]).lower()
		if query and query not in haystack:
			continue
		invoice_items.append(_invoice_target_payload(invoice, parent_name, student_labels))

	return {
		"campuses": campuses,
		"selected_campus": campuses[0] if len(campuses) == 1 else campus,
		"families": family_items[:limit],
		"invoices": invoice_items[:limit],
		"profile": profile.get("name"),
	}


def create_campus_payment_request_data(payload=None):
	reject_support_view_write()
	payload = _payload(payload)
	profile, campuses = _campus_scope(payload.get("campus"))
	if not _doctype_available():
		frappe.throw(_("Payment Collection Request is not installed. Run site migrate."))

	request_type = (payload.get("request_type") or "").strip()
	if request_type not in REQUEST_TYPES:
		frappe.throw(_("Invalid payment collection request type."))
	method = (payload.get("payment_method") or "").strip()
	if method not in PAYMENT_METHODS:
		frappe.throw(_("Select a valid payment method."))
	amount = flt(payload.get("collected_amount"))
	if amount <= 0:
		frappe.throw(_("Collected amount must be greater than zero."))
	parent = (payload.get("parent") or "").strip()
	if not parent or parent not in _campus_parent_ids(campuses):
		frappe.throw(_("The selected family is not available for this campus."), frappe.PermissionError)

	idempotency_key = (payload.get("idempotency_key") or "").strip()
	if not idempotency_key:
		frappe.throw(_("Idempotency key is required."))
	existing = frappe.db.get_value(REQUEST_DOCTYPE, {"idempotency_key": idempotency_key}, "name")
	if existing:
		return _request_payload(frappe.get_doc(REQUEST_DOCTYPE, existing))

	invoice = (payload.get("invoice") or "").strip() or None
	invoice_doc = None
	if request_type == "Invoice Payment":
		invoice_doc = _validate_invoice_for_parent(invoice, parent)
	elif invoice:
		frappe.throw(_("Store Credit Top-up requests cannot be linked to an Invoice."))

	parent_row = frappe.db.get_value("Parent", parent, ["name", "customer"], as_dict=True) or {}
	doc = frappe.get_doc({
		"doctype": REQUEST_DOCTYPE,
		"request_type": request_type,
		"status": "Pending Review",
		"campus": campuses[0],
		"parent": parent,
		"customer": parent_row.get("customer"),
		"invoice": invoice,
		"collected_amount": amount,
		"received_at": payload.get("received_at") or now_datetime(),
		"payment_method": method,
		"reference_no": (payload.get("reference_no") or "").strip(),
		"campus_admin_note": (payload.get("campus_admin_note") or "").strip(),
		"submitted_by": frappe.session.user,
		"submitted_at": now_datetime(),
		"invoice_outstanding_snapshot": _invoice_payable(invoice_doc) if invoice_doc else 0,
		"notification_status": "Queued",
		"idempotency_key": idempotency_key,
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue(
		"qas_custom.services.payment_collection_requests.send_payment_collection_request_notification_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		job_id=f"payment-collection-request:{doc.name}",
		deduplicate=True,
		request_name=doc.name,
	)
	return _request_payload(doc)


def get_campus_payment_requests_data(status=None, campus=None, query=None, limit=80):
	_profile, campuses = _campus_scope(campus)
	return _request_list(campuses=campuses, status=status, query=query, limit=limit)


def get_school_admin_payment_requests_data(status=None, campus=None, query=None, limit=120):
	_require_school_admin()
	campuses = [campus] if campus else None
	return _request_list(campuses=campuses, status=status, query=query, limit=limit, include_school_admin_identity=True)


def resolve_school_admin_payment_request_data(request_name=None, status=None, resolution_note=None, payment_entry=None, store_credit_entry=None):
	_require_school_admin()
	if not request_name or not frappe.db.exists(REQUEST_DOCTYPE, request_name):
		frappe.throw(_("Payment collection request was not found."))
	status = (status or "").strip()
	if status not in RESOLUTION_STATUSES:
		frappe.throw(_("Request status must be Processed or Rejected."))
	resolution_note = (resolution_note or "").strip()
	if status == "Rejected" and not resolution_note:
		frappe.throw(_("A rejection reason is required."))
	doc = frappe.get_doc(REQUEST_DOCTYPE, request_name)
	if doc.status != "Pending Review":
		frappe.throw(_("Only pending payment collection requests can be resolved."))
	if payment_entry and not frappe.db.exists("Payment Entry", payment_entry):
		frappe.throw(_("Payment Entry was not found."))
	if store_credit_entry and not frappe.db.exists("QAS Store Credit Ledger", store_credit_entry):
		frappe.throw(_("Store Credit ledger entry was not found."))
	doc.status = status
	doc.reviewed_by = frappe.session.user
	doc.reviewed_at = now_datetime()
	doc.resolution_note = resolution_note
	doc.payment_entry_reference = payment_entry or None
	doc.store_credit_reference = store_credit_entry or None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _request_payload(doc, include_school_admin_identity=True)


def get_pending_payment_request_count():
	if not _doctype_available():
		return 0
	return frappe.db.count(REQUEST_DOCTYPE, {"status": "Pending Review"})


def get_invoice_payment_request_summary(invoice):
	if not invoice or not _doctype_available():
		return {"pending_payment_request_count": 0, "payment_review_status": ""}
	count = frappe.db.count(REQUEST_DOCTYPE, {"invoice": invoice, "status": "Pending Review"})
	return {
		"pending_payment_request_count": count,
		"payment_review_status": "Pending Review" if count else "",
	}


def get_invoice_payment_request_summaries(invoices):
	invoices = sorted({invoice for invoice in (invoices or []) if invoice})
	if not invoices or not _doctype_available():
		return {}
	rows = frappe.get_all(
		REQUEST_DOCTYPE,
		filters={"invoice": ["in", invoices], "status": "Pending Review"},
		fields=["invoice"],
		limit_page_length=0,
	)
	counts = {}
	for row in rows:
		invoice = row.get("invoice")
		counts[invoice] = counts.get(invoice, 0) + 1
	return {
		invoice: {
			"pending_payment_request_count": counts.get(invoice, 0),
			"payment_review_status": "Pending Review" if counts.get(invoice) else "",
		}
		for invoice in invoices
	}


def send_payment_collection_request_notification_job(request_name):
	if not request_name or not frappe.db.exists(REQUEST_DOCTYPE, request_name):
		return {"sent": False, "reason": "Request not found."}
	doc = frappe.get_doc(REQUEST_DOCTYPE, request_name)
	if doc.notification_status == "Sent":
		return {"sent": True, "skipped": True}
	try:
		from qas_custom.services.maintenance import _get_school_admin_emails

		recipients = _get_school_admin_emails()
		if not recipients:
			raise RuntimeError("No active School Admin email recipients were found.")
		subject = _("Payment collection requires review – {0} – AUD {1:.2f}").format(doc.campus, flt(doc.collected_amount))
		message = _notification_message(doc)
		result = sendmail_or_skip(
			action="payment_collection_request_review",
			recipients=recipients,
			subject=subject,
			message=message,
			reference_doctype=REQUEST_DOCTYPE,
			reference_name=doc.name,
			delayed=False,
		)
		if result and result.get("skipped"):
			raise RuntimeError(result.get("reason") or "Email delivery was skipped.")
		frappe.db.set_value(REQUEST_DOCTYPE, doc.name, {"notification_status": "Sent", "notification_error": None}, update_modified=False)
		frappe.db.commit()
		return {"sent": True, "recipients": recipients}
	except Exception as exc:
		try:
			frappe.log_error(frappe.get_traceback(), f"Payment Collection Request notification failed: {request_name}")
		except Exception:
			pass
		frappe.db.set_value(REQUEST_DOCTYPE, request_name, {"notification_status": "Failed", "notification_error": str(exc)}, update_modified=False)
		frappe.db.commit()
		return {"sent": False, "reason": str(exc)}


def _request_list(campuses=None, status=None, query=None, limit=80, include_school_admin_identity=False):
	if not _doctype_available():
		return {"items": [], "pending_count": 0}
	filters = {}
	if campuses:
		filters["campus"] = ["in", campuses]
	if status:
		filters["status"] = status
	query = (query or "").strip()
	or_filters = None
	if query:
		like = f"%{query}%"
		or_filters = [
			[REQUEST_DOCTYPE, "name", "like", like],
			[REQUEST_DOCTYPE, "parent", "like", like],
			[REQUEST_DOCTYPE, "customer", "like", like],
			[REQUEST_DOCTYPE, "invoice", "like", like],
			[REQUEST_DOCTYPE, "campus", "like", like],
			[REQUEST_DOCTYPE, "submitted_by", "like", like],
			[REQUEST_DOCTYPE, "reference_no", "like", like],
			[REQUEST_DOCTYPE, "campus_admin_note", "like", like],
		]
		matching_parents = _matching_parent_ids(query)
		if matching_parents:
			or_filters.append([REQUEST_DOCTYPE, "parent", "in", sorted(matching_parents)])
	rows = frappe.get_all(
		REQUEST_DOCTYPE,
		filters=filters,
		or_filters=or_filters,
		fields=["name"],
		order_by="submitted_at desc",
		limit_page_length=min(max(cint(limit or 80), 1), 300),
	)
	items = [
		_request_payload(
			frappe.get_doc(REQUEST_DOCTYPE, row.get("name")),
			include_school_admin_identity=include_school_admin_identity,
		)
		for row in rows
	]
	items.sort(key=lambda item: item.get("status") != "Pending Review")
	pending_filters = {"status": "Pending Review"}
	if campuses:
		pending_filters["campus"] = ["in", campuses]
	return {"items": items, "pending_count": frappe.db.count(REQUEST_DOCTYPE, pending_filters)}


def _matching_parent_ids(query):
	if not query or not frappe.db.exists("DocType", "Parent"):
		return set()
	like = f"%{query}%"
	parent_fields = [field for field in ["name", "parent_name", "mobile_number", "phone", "email", "email_id"] if frappe.db.has_column("Parent", field)]
	or_filters = [["Parent", field, "like", like] for field in parent_fields]
	parents = set(frappe.get_all("Parent", or_filters=or_filters, pluck="name", limit_page_length=0)) if or_filters else set()
	if frappe.db.has_column("Student", "guardian"):
		student_fields = [field for field in ["name", "student_name", "first_name", "last_name"] if frappe.db.has_column("Student", field)]
		student_or_filters = [["Student", field, "like", like] for field in student_fields]
		if student_or_filters:
			parents.update(frappe.get_all("Student", or_filters=student_or_filters, pluck="guardian", limit_page_length=0))
	parents.discard(None)
	return parents


def _request_payload(doc, include_school_admin_identity=False):
	parent_fields = ["name", "parent_name"]
	if include_school_admin_identity:
		parent_fields.extend(
			field for field in ["mobile_number", "phone", "email", "email_id", "customer"]
			if frappe.db.has_column("Parent", field)
		)
	parent_row = frappe.db.get_value("Parent", doc.parent, parent_fields, as_dict=True) or {}
	student_labels = _students_by_parent([doc.parent]).get(doc.parent, [])
	current_invoice = None
	warnings = []
	if doc.invoice and frappe.db.exists("Sales Invoice", doc.invoice):
		invoice_doc = frappe.get_doc("Sales Invoice", doc.invoice)
		current_payable = _invoice_payable(invoice_doc)
		current_invoice = {
			"name": invoice_doc.name,
			"docstatus": cint(invoice_doc.docstatus),
			"status": invoice_doc.get("status"),
			"outstanding_amount": current_payable,
		}
		if cint(invoice_doc.docstatus) == 2 or invoice_doc.get("status") == "Cancelled":
			warnings.append(_("Invoice is now cancelled."))
		elif current_payable <= 0.005:
			warnings.append(_("Invoice no longer has an outstanding amount."))
		elif abs(current_payable - flt(doc.invoice_outstanding_snapshot)) > 0.005:
			warnings.append(_("Invoice outstanding has changed since this request was submitted."))
	payload = {
		"name": doc.name,
		"request_type": doc.request_type,
		"status": doc.status,
		"campus": doc.campus,
		"parent": doc.parent,
		"parent_name": parent_row.get("parent_name") or doc.parent,
		"customer": doc.customer,
		"students": student_labels,
		"invoice": doc.invoice,
		"collected_amount": flt(doc.collected_amount),
		"received_at": str(doc.received_at or ""),
		"payment_method": doc.payment_method,
		"reference_no": doc.reference_no or "",
		"campus_admin_note": doc.campus_admin_note or "",
		"submitted_by": doc.submitted_by,
		"submitted_at": str(doc.submitted_at or ""),
		"invoice_outstanding_snapshot": flt(doc.invoice_outstanding_snapshot),
		"current_invoice": current_invoice,
		"excess_amount": max(0, flt(doc.collected_amount) - flt((current_invoice or {}).get("outstanding_amount"))),
		"warnings": warnings,
		"notification_status": doc.notification_status,
		"notification_error": doc.notification_error or "",
		"reviewed_by": doc.reviewed_by,
		"reviewed_at": str(doc.reviewed_at or ""),
		"resolution_note": doc.resolution_note or "",
		"payment_entry_reference": doc.payment_entry_reference,
		"store_credit_reference": doc.store_credit_reference,
	}
	if include_school_admin_identity:
		payload.update({
			"parent_email": parent_row.get("email") or parent_row.get("email_id") or "",
			"parent_phone": parent_row.get("mobile_number") or parent_row.get("phone") or "",
			"customer": parent_row.get("customer") or doc.customer,
		})
	return payload


def _campus_scope(requested_campus=None):
	from qas_custom.services.campus_admin import _filter_requested_campus, _require_campus_admin_profile

	profile = _require_campus_admin_profile()
	campuses = _filter_requested_campus(profile.get("campuses") or [], requested_campus)
	if len(campuses) != 1 and not requested_campus:
		frappe.throw(_("Select one assigned campus for the payment request."))
	return profile, campuses


def _campus_parent_ids(campuses):
	parent_ids = set()
	student_ids = set()
	if frappe.db.exists("DocType", "Inquiry"):
		for row in frappe.get_all("Inquiry", filters={"campus": ["in", campuses]}, fields=["parent", "student"], limit_page_length=0):
			if row.get("parent"):
				parent_ids.add(row.get("parent"))
			if row.get("student"):
				student_ids.add(row.get("student"))
	timeslots = frappe.get_all("Weekly Timeslot", filters={"campus": ["in", campuses]}, pluck="name", limit_page_length=0) if frappe.db.exists("DocType", "Weekly Timeslot") else []
	if timeslots and frappe.db.exists("DocType", "Enrollment"):
		student_ids.update(frappe.get_all("Enrollment", filters={"weekly_timeslot": ["in", timeslots]}, pluck="student", limit_page_length=0))
	if timeslots and frappe.db.exists("DocType", "Course Sessions") and frappe.db.exists("DocType", "Class Attendance Entry"):
		sessions = frappe.get_all("Course Sessions", filters={"weekly_timeslot": ["in", timeslots]}, pluck="name", limit_page_length=0)
		if sessions:
			student_ids.update(frappe.get_all("Class Attendance Entry", filters={"course_session": ["in", sessions]}, pluck="student", limit_page_length=0))
	student_ids.discard(None)
	if student_ids and frappe.db.has_column("Student", "guardian"):
		parent_ids.update(frappe.get_all("Student", filters={"name": ["in", sorted(student_ids)]}, pluck="guardian", limit_page_length=0))
	parent_ids.discard(None)
	return parent_ids


def _parent_rows(parent_ids):
	if not parent_ids:
		return []
	fields = [field for field in ["name", "parent_name", "customer", "mobile_number", "phone", "email", "email_id"] if frappe.db.has_column("Parent", field)]
	return frappe.get_all("Parent", filters={"name": ["in", sorted(parent_ids)]}, fields=fields, order_by="parent_name asc", limit_page_length=0)


def _students_by_parent(parent_ids):
	if not parent_ids or not frappe.db.has_column("Student", "guardian"):
		return {}
	fields = [field for field in ["name", "student_name", "first_name", "last_name", "guardian"] if frappe.db.has_column("Student", field)]
	rows = frappe.get_all("Student", filters={"guardian": ["in", sorted(parent_ids)]}, fields=fields, limit_page_length=0)
	result = {}
	for row in rows:
		label = row.get("student_name") or " ".join(filter(None, [row.get("first_name"), row.get("last_name")])) or row.get("name")
		result.setdefault(row.get("guardian"), []).append(label)
	return result


def _submitted_invoice_rows(parent_ids, parent_rows):
	if not parent_ids or not frappe.db.exists("DocType", "Sales Invoice"):
		return []
	fields = [field for field in ["name", "parent", "customer", "customer_name", "student_summary", "docstatus", "status", "outstanding_amount", "grand_total"] if frappe.db.has_column("Sales Invoice", field)]
	rows = []
	if frappe.db.has_column("Sales Invoice", "parent"):
		rows.extend(frappe.get_all("Sales Invoice", filters={"parent": ["in", sorted(parent_ids)], "docstatus": 1}, fields=fields, limit_page_length=0))
	customers = sorted({row.get("customer") for row in parent_rows if row.get("customer")})
	if customers:
		rows.extend(frappe.get_all("Sales Invoice", filters={"customer": ["in", customers], "docstatus": 1}, fields=fields, limit_page_length=0))
	parent_by_customer = {row.get("customer"): row.get("name") for row in parent_rows if row.get("customer")}
	seen = set()
	result = []
	for row in rows:
		if row.get("name") in seen:
			continue
		seen.add(row.get("name"))
		if not row.get("parent"):
			row["parent"] = parent_by_customer.get(row.get("customer"))
		if row.get("parent") in parent_ids:
			result.append(row)
	return result


def _validate_invoice_for_parent(invoice, parent):
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		frappe.throw(_("Invoice was not found."))
	doc = frappe.get_doc("Sales Invoice", invoice)
	if cint(doc.docstatus) != 1:
		frappe.throw(_("Only submitted Invoices can be reported as collected."))
	parent_customer = frappe.db.get_value("Parent", parent, "customer")
	if doc.get("parent") != parent and (not parent_customer or doc.get("customer") != parent_customer):
		frappe.throw(_("Invoice does not belong to the selected family."), frappe.PermissionError)
	return doc


def _invoice_target_payload(invoice, parent_name, students):
	return {
		"invoice": invoice.get("name"),
		"parent": invoice.get("parent"),
		"parent_name": parent_name,
		"customer": invoice.get("customer"),
		"customer_name": invoice.get("customer_name"),
		"students": students,
		"status": invoice.get("status"),
		"outstanding_amount": flt(invoice.get("outstanding_amount")),
		"grand_total": flt(invoice.get("grand_total")),
		**get_invoice_payment_request_summary(invoice.get("name")),
	}


def _invoice_payable(invoice_doc):
	return max(0, flt(get_invoice_payable_amount(invoice_doc))) if invoice_doc else 0


def _require_school_admin():
	if frappe.session.user == "Guest" or not set(frappe.get_roles(frappe.session.user)).intersection({"School Admin", "System Manager"}):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)


def _payload(payload):
	if isinstance(payload, str):
		return json.loads(payload) if payload.strip() else {}
	return payload or {}


def _doctype_available():
	return bool(frappe.db.exists("DocType", REQUEST_DOCTYPE))


def _notification_message(doc):
	rows = [
		("Request", doc.name), ("Campus", doc.campus), ("Submitted by", doc.submitted_by),
		("Parent", doc.parent), ("Type", doc.request_type), ("Invoice", doc.invoice or "-"),
		("Collected amount", f"AUD {flt(doc.collected_amount):.2f}"), ("Payment method", doc.payment_method),
		("Received at", doc.received_at), ("Reference", doc.reference_no or "-"),
		("Note", doc.campus_admin_note or "-"),
	]
	body = "".join(f"<tr><td style='padding:6px;color:#64748b'>{escape(str(label))}</td><td style='padding:6px;font-weight:700'>{escape(str(value))}</td></tr>" for label, value in rows)
	return f"<p>A Campus Admin reported a payment collection that requires School Admin review.</p><table>{body}</table><p><a href='https://portal.queenslandartschool.com/school-admin'>Open School Admin</a></p>"
