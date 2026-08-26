from __future__ import annotations

import base64
import json
import os
import re
from datetime import timedelta
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

from qas_custom.services.inquiry import _submission_data_rows
from qas_custom.services.maintenance import _issue, _make_issue_key, record_data_issue, resolve_data_issue
from qas_custom.services.school_admin import _limit, _require_school_admin
from qas_custom.utils.environment import sendmail_or_skip


EVENT_DOCTYPE = "Google Conversion Sync Event"
SHEET_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
EXPECTED_HEADERS = [
	"event_name",
	"conversion_date_time",
	"conversion_value",
	"currency_code",
	"order_id",
	"gclid",
	"gbraid",
	"wbraid",
	"email",
	"phone",
	"submission_id",
	"upload_status",
	"upload_error",
	"last_upload_time",
]
VALID_STATUSES = {"Pending", "Sent", "Failed"}
MAX_ATTEMPTS = 5
RETRY_MINUTES = (1, 5, 15, 60, 240)


def capture_payment_entry_submit(doc, method=None):
	if not _capture_enabled() or cint(doc.get("docstatus")) != 1:
		return
	invoice_names = sorted(
		{
			row.get("reference_name")
			for row in doc.get("references", [])
			if row.get("reference_doctype") == "Sales Invoice" and row.get("reference_name")
		}
	)
	if not invoice_names:
		return
	frappe.enqueue(
		"qas_custom.services.google_conversion_sync.capture_trial_invoice_payment_events_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"qas-google-conversion-payment-{doc.name}",
		payment_entry=doc.name,
		invoice_names=invoice_names,
	)


def capture_inquiry_conversion_update(doc, method=None):
	if not _capture_enabled():
		return
	if doc.get("inquiry_type") != "Trial Lesson" or doc.get("status") != "Converted" or not doc.get("converted_enrollment"):
		return
	if hasattr(doc, "has_value_changed") and not (
		doc.has_value_changed("status") or doc.has_value_changed("converted_enrollment")
	):
		return
	frappe.enqueue(
		"qas_custom.services.google_conversion_sync.capture_enrolled_student_event_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"qas-google-conversion-enrollment-{doc.name}",
		inquiry=doc.name,
	)


def capture_trial_invoice_payment_events_job(payment_entry=None, invoice_names=None):
	if not _capture_enabled() or not _doctype_available(EVENT_DOCTYPE):
		return {"created": [], "skipped": list(invoice_names or [])}
	created = []
	skipped = []
	for invoice_name in sorted(set(invoice_names or [])):
		payload = _trial_invoice_paid_event(invoice_name)
		if not payload:
			skipped.append(invoice_name)
			continue
		event = _create_sync_event(payload)
		if event:
			created.append(event)
	return {"created": created, "skipped": skipped, "payment_entry": payment_entry}


def capture_enrolled_student_event_job(inquiry=None):
	if not _capture_enabled() or not _doctype_available(EVENT_DOCTYPE):
		return {"created": None, "inquiry": inquiry}
	payload = _enrolled_student_event(inquiry)
	if not payload:
		return {"created": None, "inquiry": inquiry}
	return {"created": _create_sync_event(payload), "inquiry": inquiry}


def run_google_conversion_sync_event(event=None):
	if not event or not _doctype_available(EVENT_DOCTYPE):
		return {"skipped": True, "reason": "Sync event is unavailable."}
	with frappe.cache.lock(f"qas-google-conversion-sync:{event}", timeout=90, blocking_timeout=10):
		return _run_google_conversion_sync_event(event)


def _run_google_conversion_sync_event(event):
	frappe.db.sql(f"select name from `tab{EVENT_DOCTYPE}` where name = %s for update", (event,))
	doc = frappe.get_doc(EVENT_DOCTYPE, event)
	if doc.status == "Sent":
		return {"event": event, "status": "Sent", "skipped": True}

	doc.attempt_count = cint(doc.attempt_count) + 1
	doc.last_attempted_at = now_datetime()
	doc.next_retry_at = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		config = _google_config(require_enabled=True, require_credentials=True)
		client = GoogleSheetsClient(config)
		client.validate_headers()
		payload = json.loads(doc.payload_json or "{}")
		if client.event_exists(payload.get("event_name"), payload.get("order_id")):
			destination_range = "existing:event_name+order_id"
		else:
			destination_range = client.append_row(_sheet_row(payload))
		_mark_event_sent(event, destination_range)
		return {"event": event, "status": "Sent", "destination_range": destination_range}
	except Exception as exc:
		return _record_delivery_failure(event, exc)


def run_google_conversion_sync_recovery():
	if not _capture_enabled() or not _doctype_available(EVENT_DOCTYPE):
		return {"skipped": True, "reason": "Google conversion sync is disabled or unavailable."}

	now = now_datetime()
	due = set(
		frappe.get_all(
			EVENT_DOCTYPE,
			filters={"status": "Pending", "next_retry_at": ["<=", now]},
			pluck="name",
			limit=100,
		)
	)
	due.update(
		frappe.get_all(
			EVENT_DOCTYPE,
			filters={"status": "Pending", "next_retry_at": ["is", "not set"]},
			pluck="name",
			limit=100,
		)
	)
	for event in sorted(due):
		_enqueue_sync_event(event)

	recovered = _recover_missing_source_events(now)
	return {"queued": len(due), "recovered": recovered}


def get_school_admin_google_conversion_sync_events_data(status=None, limit_start=0, limit=50):
	_require_school_admin()
	if not _doctype_available(EVENT_DOCTYPE):
		return {"items": [], "total": 0, "config": _public_config()}
	status = str(status or "").strip()
	if status and status not in VALID_STATUSES:
		frappe.throw(_("Invalid Google conversion sync status."))
	filters = {"status": status} if status else {}
	limit_start = max(cint(limit_start), 0)
	limit = _limit(limit, default=50, max_value=200)
	fields = [
		"name",
		"event_key",
		"event_name",
		"order_id",
		"status",
		"inquiry",
		"invoice",
		"payment_entry",
		"enrollment",
		"conversion_date_time",
		"conversion_value",
		"attempt_count",
		"next_retry_at",
		"last_attempted_at",
		"sent_at",
		"destination_range",
		"last_error",
		"creation",
	]
	items = frappe.get_all(
		EVENT_DOCTYPE,
		filters=filters,
		fields=fields,
		order_by="creation desc",
		limit_start=limit_start,
		limit_page_length=limit,
	)
	return {
		"items": [dict(row) for row in items],
		"total": frappe.db.count(EVENT_DOCTYPE, filters=filters),
		"config": _public_config(),
	}


def retry_school_admin_google_conversion_sync_event_data(event=None):
	_require_school_admin()
	_google_config(require_enabled=True, require_credentials=False)
	if not event or not frappe.db.exists(EVENT_DOCTYPE, event):
		frappe.throw(_("Google conversion sync event was not found."))
	doc = frappe.get_doc(EVENT_DOCTYPE, event)
	if doc.status != "Failed":
		frappe.throw(_("Only Failed Google conversion sync events can be retried."))
	doc.status = "Pending"
	doc.attempt_count = 0
	doc.next_retry_at = now_datetime()
	doc.last_error = ""
	doc.error_notification_sent = 0
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	_enqueue_sync_event(doc.name)
	return {"event": doc.name, "status": "Pending"}


def validate_school_admin_google_conversion_sync_data():
	_require_school_admin()
	config = _google_config(require_enabled=False, require_credentials=True)
	client = GoogleSheetsClient(config)
	headers = client.validate_headers()
	return {
		"ok": True,
		"spreadsheet_id": config["spreadsheet_id"],
		"sheet_name": config["sheet_name"],
		"service_account_email": config["service_account_info"].get("client_email"),
		"headers": headers,
		"enabled": config["enabled"],
		"enabled_at": config["enabled_at"],
	}


def _trial_invoice_paid_event(invoice_name):
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return None
	invoice = frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		["name", "docstatus", "status", "grand_total", "outstanding_amount"],
		as_dict=True,
	)
	if not invoice or cint(invoice.docstatus) != 1 or str(invoice.status or "").lower() == "cancelled":
		return None
	if flt(invoice.outstanding_amount) > 0.005:
		return None
	inquiries = frappe.get_all(
		"Inquiry",
		filters={"trial_invoice": invoice_name, "inquiry_type": "Trial Lesson"},
		fields=_inquiry_event_fields(),
		limit=2,
	)
	if len(inquiries) != 1:
		return None
	payment_entry = _latest_submitted_payment_entry(invoice_name)
	if not payment_entry:
		return None
	inquiry = inquiries[0]
	return _event_payload(
		inquiry,
		event_key=f"trial_invoice_paid:{invoice_name}",
		event_name="QAS - Trial Invoice Paid",
		order_id=f"TRIAL-{inquiry.name}",
		conversion_date_time=payment_entry.creation,
		conversion_value=_trial_conversion_value(invoice.grand_total),
		source_doctype="Sales Invoice",
		source_document=invoice_name,
		invoice=invoice_name,
		payment_entry=payment_entry.name,
	)


def _enrolled_student_event(inquiry_name):
	if not inquiry_name or not frappe.db.exists("Inquiry", inquiry_name):
		return None
	inquiry = frappe.db.get_value("Inquiry", inquiry_name, _inquiry_event_fields(), as_dict=True)
	if not inquiry or inquiry.inquiry_type != "Trial Lesson" or inquiry.status != "Converted" or not inquiry.converted_enrollment:
		return None
	conversion_time = _inquiry_conversion_time(inquiry.name, inquiry.converted_enrollment) or inquiry.modified
	return _event_payload(
		inquiry,
		event_key=f"enrolled_student:{inquiry.name}:{inquiry.converted_enrollment}",
		event_name="QAS - Enrolled Student",
		order_id=f"ENROL-{inquiry.name}",
		conversion_date_time=conversion_time,
		conversion_value=400,
		source_doctype="Enrollment",
		source_document=inquiry.converted_enrollment,
		enrollment=inquiry.converted_enrollment,
	)


def _event_payload(
	inquiry,
	*,
	event_key,
	event_name,
	order_id,
	conversion_date_time,
	conversion_value,
	source_doctype,
	source_document,
	invoice=None,
	payment_entry=None,
	enrollment=None,
):
	click_ids = _extract_click_ids(inquiry.get("raw_webhook_payload"))
	return {
		"event_key": event_key,
		"event_name": event_name,
		"conversion_date_time": _format_conversion_datetime(conversion_date_time),
		"conversion_value": flt(conversion_value, 2),
		"currency_code": "AUD",
		"order_id": order_id,
		"gclid": click_ids.get("gclid", ""),
		"gbraid": click_ids.get("gbraid", ""),
		"wbraid": click_ids.get("wbraid", ""),
		"email": str(inquiry.get("contact_email") or "").strip().lower(),
		"phone": _normalise_phone(inquiry.get("contact_phone")),
		"submission_id": _submission_id(inquiry),
		"upload_status": "pending_review",
		"upload_error": "",
		"last_upload_time": "",
		"source_doctype": source_doctype,
		"source_document": source_document,
		"inquiry": inquiry.get("name"),
		"invoice": invoice or "",
		"payment_entry": payment_entry or "",
		"enrollment": enrollment or "",
	}


def _create_sync_event(payload):
	event_key = payload["event_key"]
	existing = frappe.db.get_value(EVENT_DOCTYPE, {"event_key": event_key}, "name")
	if existing:
		return existing
	doc = frappe.new_doc(EVENT_DOCTYPE)
	doc.event_key = event_key
	doc.event_name = payload["event_name"]
	doc.order_id = payload["order_id"]
	doc.status = "Pending"
	doc.source_doctype = payload.get("source_doctype")
	doc.source_document = payload.get("source_document")
	doc.inquiry = payload.get("inquiry")
	doc.invoice = payload.get("invoice")
	doc.payment_entry = payload.get("payment_entry")
	doc.enrollment = payload.get("enrollment")
	doc.conversion_date_time = get_datetime(payload["conversion_date_time"])
	doc.conversion_value = payload["conversion_value"]
	doc.payload_json = json.dumps(_public_row_payload(payload), ensure_ascii=False, separators=(",", ":"))
	doc.next_retry_at = now_datetime()
	try:
		doc.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		doc.name = frappe.db.get_value(EVENT_DOCTYPE, {"event_key": event_key}, "name")
	frappe.db.commit()
	_enqueue_sync_event(doc.name)
	return doc.name


def _enqueue_sync_event(event):
	if not event:
		return
	frappe.enqueue(
		"qas_custom.services.google_conversion_sync.run_google_conversion_sync_event",
		queue="short",
		timeout=300,
		deduplicate=True,
		job_id=f"qas-google-conversion-sync-{event}",
		event=event,
	)


def _mark_event_sent(event, destination_range):
	doc = frappe.get_doc(EVENT_DOCTYPE, event)
	doc.status = "Sent"
	doc.sent_at = now_datetime()
	doc.destination_range = destination_range
	doc.next_retry_at = None
	doc.last_error = ""
	doc.save(ignore_permissions=True)
	resolve_data_issue(_sync_issue_key(doc.event_key))
	frappe.db.commit()


def _record_delivery_failure(event, exc):
	doc = frappe.get_doc(EVENT_DOCTYPE, event)
	doc.last_error = _safe_error(exc)
	if cint(doc.attempt_count) >= MAX_ATTEMPTS:
		doc.status = "Failed"
		doc.next_retry_at = None
	else:
		doc.status = "Pending"
		minutes = RETRY_MINUTES[min(max(cint(doc.attempt_count) - 1, 0), len(RETRY_MINUTES) - 1)]
		doc.next_retry_at = add_to_date(now_datetime(), minutes=minutes, as_datetime=True)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	if doc.status == "Failed" and not cint(doc.error_notification_sent):
		_record_failed_event_issue(doc)
		if _send_failed_event_email(doc):
			doc = frappe.get_doc(EVENT_DOCTYPE, event)
			doc.error_notification_sent = 1
			doc.save(ignore_permissions=True)
			frappe.db.commit()
	return {
		"event": event,
		"status": doc.status,
		"attempt_count": cint(doc.attempt_count),
		"error": doc.last_error,
	}


def _record_failed_event_issue(doc):
	record_data_issue(
		_issue(
			key_parts=["google-conversion-sync", doc.event_key],
			issue_type="Google Conversion Sync",
			severity="Critical",
			source_doctype=doc.source_doctype,
			source_document=doc.source_document,
			related_doctype=EVENT_DOCTYPE,
			related_document=doc.name,
			description=_("Google conversion event {0} failed after {1} attempts: {2}").format(
				doc.order_id,
				doc.attempt_count,
				doc.last_error,
			),
			suggested_action=_("Review Google access and configuration, then retry the event from School Admin I&O."),
		),
		notify=False,
	)


def _send_failed_event_email(doc):
	try:
		recipient = str(
			_config_value("QAS_GOOGLE_CONVERSION_FAILURE_EMAIL", "qas_google_conversion_failure_email")
			or "Roger130@gmail.com"
		).strip()
		sendmail_or_skip(
			action="google_conversion_sync_failure",
			recipients=[recipient],
			subject=_("QAS Google conversion sync failed: {0}").format(doc.order_id),
			message="<br>".join(
				[
					_("A Google conversion event could not be added to the Sheet."),
					_("Event: {0}").format(doc.event_name),
					_("Order ID: {0}").format(doc.order_id),
					_("Attempts: {0}").format(doc.attempt_count),
					_("Last attempted: {0}").format(doc.last_attempted_at),
					_("Error: {0}").format(doc.last_error),
				],
			),
			now=False,
		)
		return True
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Google conversion sync failure email failed")
		return False


def _recover_missing_source_events(now):
	enabled_at = _capture_enabled_at()
	if not enabled_at:
		return {"payment_entries": 0, "converted_inquiries": 0}
	lower_bound = max(enabled_at, now - timedelta(days=7))
	payment_entries = frappe.get_all(
		"Payment Entry",
		filters={"docstatus": 1, "creation": [">=", lower_bound]},
		fields=["name"],
		order_by="creation asc",
		limit=500,
	)
	for row in payment_entries:
		doc = frappe.get_doc("Payment Entry", row.name)
		invoice_names = sorted(
			{
				reference.get("reference_name")
				for reference in doc.get("references", [])
				if reference.get("reference_doctype") == "Sales Invoice" and reference.get("reference_name")
			}
		)
		if invoice_names:
			capture_trial_invoice_payment_events_job(payment_entry=doc.name, invoice_names=invoice_names)

	inquiries = frappe.get_all(
		"Inquiry",
		filters={
			"inquiry_type": "Trial Lesson",
			"status": "Converted",
			"converted_enrollment": ["is", "set"],
			"modified": [">=", lower_bound],
		},
		fields=["name"],
		order_by="modified asc",
		limit=500,
	)
	for row in inquiries:
		capture_enrolled_student_event_job(row.name)
	return {"payment_entries": len(payment_entries), "converted_inquiries": len(inquiries)}


def _latest_submitted_payment_entry(invoice_name):
	references = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": invoice_name},
		fields=["parent", "allocated_amount"],
		limit_page_length=0,
	)
	parents = sorted({row.parent for row in references if row.parent and flt(row.allocated_amount) > 0})
	if not parents:
		return None
	rows = frappe.get_all(
		"Payment Entry",
		filters={"name": ["in", parents], "docstatus": 1},
		fields=["name", "creation"],
		order_by="creation desc, name desc",
		limit=1,
	)
	return rows[0] if rows else None


def _inquiry_conversion_time(inquiry, enrollment):
	if not _doctype_available("Inquiry Note"):
		return None
	filters = {"inquiry": inquiry}
	if frappe.get_meta("Inquiry Note").has_field("source_doctype"):
		filters["source_doctype"] = "Enrollment"
	if frappe.get_meta("Inquiry Note").has_field("source_document"):
		filters["source_document"] = enrollment
	rows = frappe.get_all(
		"Inquiry Note",
		filters=filters,
		fields=["creation", "edited_at"],
		order_by="creation desc",
		limit=1,
	)
	return (rows[0].get("creation") or rows[0].get("edited_at")) if rows else None


def _inquiry_event_fields():
	fields = [
		"name",
		"inquiry_type",
		"status",
		"converted_enrollment",
		"contact_email",
		"contact_phone",
		"external_submission_id",
		"external_form_id",
		"external_serial_number",
		"raw_webhook_payload",
		"modified",
	]
	meta = frappe.get_meta("Inquiry")
	return [field for field in fields if field == "name" or meta.has_field(field)]


def _trial_conversion_value(grand_total):
	amount = flt(grand_total, 2)
	return 68.0 if amount > 100 else amount


def _extract_click_ids(raw_payload):
	result = {"gclid": "", "gbraid": "", "wbraid": ""}
	for item in _submission_data_rows(raw_payload):
		label = str(item.get("label") or "").lower().split("·")[-1]
		key = re.sub(r"[^a-z0-9]", "", label)
		if key in result and not result[key]:
			result[key] = str(item.get("value") or "").strip()
	return result


def _normalise_phone(value):
	text = str(value or "").strip()
	if not text:
		return ""
	digits = re.sub(r"\D", "", text)
	if text.startswith("+") and 8 <= len(digits) <= 15:
		return f"+{digits}"
	if len(digits) == 10 and digits.startswith("0"):
		return f"+61{digits[1:]}"
	if digits.startswith("61") and 8 <= len(digits) <= 15:
		return f"+{digits}"
	return text


def _submission_id(inquiry):
	existing = str(inquiry.get("external_submission_id") or "").strip()
	if existing:
		return existing
	form_id = str(inquiry.get("external_form_id") or "").strip()
	serial = str(inquiry.get("external_serial_number") or "").strip()
	return f"fluent_form:{form_id}:{serial}" if form_id and serial else ""


def _format_conversion_datetime(value):
	return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S")


def _public_row_payload(payload):
	return {header: payload.get(header, "") for header in EXPECTED_HEADERS}


def _sheet_row(payload):
	return [payload.get(header, "") for header in EXPECTED_HEADERS]


def _safe_error(exc):
	message = re.sub(r"\s+", " ", str(exc or "Google Sheets request failed.")).strip()
	message = re.sub(r'"private_key"\s*:\s*"[^"]+"', '"private_key":"[redacted]"', message, flags=re.I)
	message = re.sub(
		r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----",
		"[redacted private key]",
		message,
		flags=re.I | re.S,
	)
	return message[:500] or "Google Sheets request failed."


def _sync_issue_key(event_key):
	return _make_issue_key(["google-conversion-sync", event_key])


def _capture_enabled():
	enabled = cint(_config_value("QAS_GOOGLE_CONVERSION_ENABLED", "qas_google_conversion_enabled") or 0) == 1
	enabled_at = _capture_enabled_at()
	return bool(enabled and enabled_at and now_datetime() >= enabled_at)


def _capture_enabled_at():
	value = str(_config_value("QAS_GOOGLE_CONVERSION_ENABLED_AT", "qas_google_conversion_enabled_at") or "").strip()
	if not value:
		return None
	try:
		return get_datetime(value)
	except Exception:
		return None


def _public_config():
	enabled = cint(_config_value("QAS_GOOGLE_CONVERSION_ENABLED", "qas_google_conversion_enabled") or 0) == 1
	enabled_at = str(_config_value("QAS_GOOGLE_CONVERSION_ENABLED_AT", "qas_google_conversion_enabled_at") or "").strip()
	spreadsheet_id = str(
		_config_value("QAS_GOOGLE_CONVERSION_SPREADSHEET_ID", "qas_google_conversion_spreadsheet_id") or ""
	).strip()
	sheet_name = str(
		_config_value("QAS_GOOGLE_CONVERSION_SHEET_NAME", "qas_google_conversion_sheet_name") or "Google Ads Upload"
	).strip()
	failure_email = str(
		_config_value("QAS_GOOGLE_CONVERSION_FAILURE_EMAIL", "qas_google_conversion_failure_email")
		or "Roger130@gmail.com"
	).strip()
	try:
		has_credentials = bool(_service_account_info(required=False))
	except Exception:
		has_credentials = False
	return {
		"enabled": enabled,
		"configured": bool(spreadsheet_id and sheet_name and has_credentials),
		"enabled_at": enabled_at,
		"sheet_name": sheet_name,
		"failure_email": failure_email,
	}


def _google_config(*, require_enabled, require_credentials):
	enabled = cint(_config_value("QAS_GOOGLE_CONVERSION_ENABLED", "qas_google_conversion_enabled") or 0) == 1
	enabled_at = str(_config_value("QAS_GOOGLE_CONVERSION_ENABLED_AT", "qas_google_conversion_enabled_at") or "").strip()
	spreadsheet_id = str(
		_config_value("QAS_GOOGLE_CONVERSION_SPREADSHEET_ID", "qas_google_conversion_spreadsheet_id") or ""
	).strip()
	sheet_name = str(
		_config_value("QAS_GOOGLE_CONVERSION_SHEET_NAME", "qas_google_conversion_sheet_name") or "Google Ads Upload"
	).strip()
	failure_email = str(
		_config_value("QAS_GOOGLE_CONVERSION_FAILURE_EMAIL", "qas_google_conversion_failure_email")
		or "Roger130@gmail.com"
	).strip()
	service_account_info = _service_account_info(required=require_credentials)

	if require_enabled and not enabled:
		frappe.throw(_("Google conversion sync is disabled."))
	if enabled and not enabled_at:
		frappe.throw(_("Google conversion sync enabled time is required."))
	if enabled_at:
		try:
			get_datetime(enabled_at)
		except Exception:
			frappe.throw(_("Google conversion sync enabled time is invalid."))
	if not spreadsheet_id:
		frappe.throw(_("Google conversion spreadsheet ID is required."))
	if not sheet_name:
		frappe.throw(_("Google conversion sheet name is required."))

	return {
		"enabled": enabled,
		"enabled_at": enabled_at,
		"spreadsheet_id": spreadsheet_id,
		"sheet_name": sheet_name,
		"failure_email": failure_email,
		"service_account_info": service_account_info,
		"has_credentials": bool(service_account_info),
	}


def _service_account_info(required):
	encoded = _config_value(
		"QAS_GOOGLE_CONVERSION_SERVICE_ACCOUNT_JSON_B64",
		"qas_google_conversion_service_account_json_b64",
	)
	raw = _config_value(
		"QAS_GOOGLE_CONVERSION_SERVICE_ACCOUNT_JSON",
		"qas_google_conversion_service_account_json",
	)
	try:
		if encoded:
			raw = base64.b64decode(str(encoded)).decode("utf-8")
		if isinstance(raw, dict):
			info = raw
		elif raw:
			info = json.loads(str(raw))
		else:
			info = None
	except Exception:
		frappe.throw(_("Google Service Account credential is invalid."))
	if required and not info:
		frappe.throw(_("Google Service Account credential is required."))
	if info and (not info.get("client_email") or not info.get("private_key")):
		frappe.throw(_("Google Service Account credential is incomplete."))
	return info


def _config_value(environment_key, conf_key):
	return os.getenv(environment_key) or frappe.conf.get(conf_key) or frappe.conf.get(environment_key)


def _doctype_available(doctype):
	return bool(frappe.db.exists("DocType", doctype))


class GoogleSheetsClient:
	def __init__(self, config):
		from google.auth.transport.requests import AuthorizedSession
		from google.oauth2 import service_account

		credentials = service_account.Credentials.from_service_account_info(
			config["service_account_info"],
			scopes=[SHEET_SCOPE],
		)
		self.session = AuthorizedSession(credentials)
		self.spreadsheet_id = config["spreadsheet_id"]
		self.sheet_name = config["sheet_name"]

	def validate_headers(self):
		rows = self.get_values("A1:N1")
		headers = rows[0] if rows else []
		if headers != EXPECTED_HEADERS:
			raise ValueError("Google Ads Upload headers do not match the required A:N schema.")
		return headers

	def event_exists(self, event_name, order_id):
		if not event_name or not order_id:
			return False
		for row in self.get_values("A2:E"):
			if len(row) >= 5 and str(row[0]) == str(event_name) and str(row[4]) == str(order_id):
				return True
		return False

	def append_row(self, row):
		range_name = f"'{self.sheet_name}'!A:N"
		url = self._values_url(range_name) + ":append"
		response = self.session.post(
			url,
			params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
			json={"majorDimension": "ROWS", "values": [row]},
			timeout=30,
		)
		self._raise_for_status(response)
		body = response.json()
		return body.get("updates", {}).get("updatedRange") or "appended"

	def get_values(self, cell_range):
		range_name = f"'{self.sheet_name}'!{cell_range}"
		response = self.session.get(self._values_url(range_name), timeout=30)
		self._raise_for_status(response)
		return response.json().get("values", [])

	def _values_url(self, range_name):
		return "https://sheets.googleapis.com/v4/spreadsheets/{0}/values/{1}".format(
			quote(self.spreadsheet_id, safe=""),
			quote(range_name, safe=""),
		)

	@staticmethod
	def _raise_for_status(response):
		try:
			response.raise_for_status()
		except Exception as exc:
			body = str(getattr(response, "text", "") or "")[:300]
			raise RuntimeError(f"Google Sheets request failed ({getattr(response, 'status_code', 'unknown')}): {body}") from exc
