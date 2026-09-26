"""A separate terms notice after conversion; never a parent acceptance gate."""
from hashlib import sha256

import frappe
from frappe.utils import escape_html

from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.notifications.commands import (
    _create_notification_log,
    _invoice_recipient,
    _mark_notification_failed,
    _mark_notification_queued,
    _mark_notification_sent,
    _notification_log_available,
)
from qas_custom.utils.environment import sendmail_or_skip


def queue_enrollment_terms_notice(enrollment, invoice):
    """Snapshot the notice in the conversion transaction; deliver only after commit."""
    log_name = None
    try:
        settings = get_invoice_settings()
        terms = str(settings.get("enrollment_terms") or "").strip()
        if not terms:
            return {"queued": False, "skipped": True, "reason": "Enrollment terms are not configured."}
        if not _notification_log_available() or not frappe.get_meta("Notification Log").has_field("event_key"):
            raise RuntimeError("Notification Log event keys are unavailable. Run the site migration.")
        event_key = "enrollment_terms:" + sha256(enrollment.name.encode()).hexdigest()[:24]
        existing = frappe.db.get_value("Notification Log", {"event_key": event_key}, "name")
        if existing:
            return {"queued": False, "duplicate": True, "notification_log": existing}
        recipient = _invoice_recipient(invoice)
        reply_to = str(settings.get("school_email") or "").strip()
        if not recipient.get("email") or not reply_to:
            raise RuntimeError("A parent email and school reply email are required for the terms notice.")
        subject = "{0} - Enrollment Terms and Conditions - {1}".format(
            settings.get("school_name") or "Queensland Art School", enrollment.name
        )
        message = (
            "<p>Please review the Terms and Conditions for your child's enrollment ({0}).</p>"
            "<div style='white-space:pre-wrap'>{1}</div>"
            "<p>If you do not accept these terms, or have any questions, please reply to this email "
            "to contact the school.</p>"
        ).format(escape_html(enrollment.name), escape_html(terms))
        log_name = _create_notification_log(
            event_key=event_key, recipient=recipient, subject=subject, message=message,
            document_type="Enrollment", document_name=enrollment.name,
        )
        if not log_name:
            raise RuntimeError("Could not save the enrollment terms notice.")
        _mark_notification_queued(log_name)
        frappe.enqueue(
            "qas_custom.modules.notifications.enrollment_terms.send_enrollment_terms_notice_job",
            queue="short", timeout=300, enqueue_after_commit=True,
            job_id=event_key.replace(":", "-"), deduplicate=True,
            notification_log=log_name, reply_to=reply_to,
        )
        return {"queued": True, "notification_log": log_name}
    except frappe.DuplicateEntryError:
        return {"queued": False, "duplicate": True}
    except Exception:
        # Conversion must remain successful even if configuration or the queue is unavailable.
        _record_failure(log_name, "Enrollment terms notice could not be queued.")
        return {"queued": False, "reason": "Enrollment terms notice could not be queued.", "notification_log": log_name}


def send_enrollment_terms_notice_job(notification_log, reply_to):
    """A failed delivery can retry this saved notice; sent notices are never resent."""
    log = frappe.get_doc("Notification Log", notification_log, for_update=True)
    if not str(log.get("event_key") or "").startswith("enrollment_terms:"):
        return {"sent": False, "skipped": True, "reason": "Not an enrollment terms notice."}
    if any(log.get(field) == "Sent" for field in ("status", "delivery_status", "email_status")):
        return {"sent": False, "duplicate": True}
    recipient = log.get("email_to") or log.get("recipient_email")
    if not recipient or not reply_to:
        _record_failure(log.name, "Missing parent email or school reply address.")
        return {"sent": False, "notification_log": log.name}
    try:
        result = sendmail_or_skip(
            action="enrollment_terms_notice", recipients=[recipient], reply_to=reply_to,
            subject=log.subject, message=log.email_content, delayed=False,
            reference_doctype="Enrollment", reference_name=log.document_name,
        )
        if result and result.get("skipped"):
            _mark_notification_failed(log.name, result.get("reason") or "Email sending is disabled.")
            return {"sent": False, "skipped": True, "notification_log": log.name}
        _mark_notification_sent(log.name)
        return {"sent": True, "notification_log": log.name}
    except Exception:
        _record_failure(log.name, "Enrollment terms email delivery failed.")
        return {"sent": False, "notification_log": log.name}


def _record_failure(log_name, reason):
    try:
        if log_name:
            _mark_notification_failed(log_name, reason)
        frappe.log_error(frappe.get_traceback(), reason)
    except Exception:
        pass
