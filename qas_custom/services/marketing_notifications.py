"""Minimal website trial notifications, independent of internal admin emails."""
from hashlib import sha256

import frappe
from frappe.utils import cint, escape_html, validate_email_address

from qas_custom.utils.environment import outbound_email_enabled, scheduler_enabled, sendmail_or_skip

SETTINGS = "Marketing Notification Settings"
DELIVERY = "Marketing Trial Notification"
SUBJECT = "New website trial enquiry"


def validate_settings(enabled, recipient):
    if str(enabled) not in {"0", "1", "False", "True", "None", ""}:
        frappe.throw("Enabled must be 0 or 1.")
    enabled = 1 if enabled in (True, 1, "1", "True") else 0
    recipient = str(recipient or "").strip().lower()
    if recipient:
        # Frappe accepts lists/display names; this feature accepts one bare address.
        if any(char.isspace() or char in ",;<>" for char in recipient):
            frappe.throw("Enter one email address, without a name or recipient list.")
        validate_email_address(recipient, throw=True)
    if enabled and not recipient:
        frappe.throw("Enter the marketing email before enabling notifications.")
    return enabled, recipient


def get_settings():
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    doc = frappe.get_single(SETTINGS)
    return {"enabled": cint(doc.enabled), "recipient": doc.recipient or ""}


def save_settings(enabled=0, recipient=None):
    from qas_custom.services.school_admin import _require_school_admin
    from qas_custom.services.support_view import reject_support_view_write
    _require_school_admin()
    reject_support_view_write()
    enabled, recipient = validate_settings(enabled, recipient)
    doc = frappe.get_single(SETTINGS)
    doc.enabled, doc.recipient = enabled, recipient
    doc.save(ignore_permissions=True)
    return {"enabled": enabled, "recipient": recipient}


def record_webhook_trial(inquiry):
    """Called only by the authenticated trial webhook, before its transaction commits."""
    frappe.db.savepoint("marketing_trial_record")
    try:
        settings = frappe.get_single(SETTINGS)
        if not cint(settings.enabled) or not settings.recipient:
            return
        doc = frappe.get_doc("Inquiry", inquiry)
        if doc.inquiry_type != "Trial Lesson" or not doc.get("external_submission_id"):
            return
        name = sha256(str(inquiry).encode()).hexdigest()
        if frappe.db.exists(DELIVERY, name):
            return
        delivery = frappe.get_doc({
            "doctype": DELIVERY, "name": name, "inquiry": inquiry,
            "recipient": settings.recipient, "parent_name": doc.contact_name or "",
            "status": "Pending",
        }).insert(ignore_permissions=True, set_name=name)
    except Exception:
        frappe.db.rollback(save_point="marketing_trial_record")
        _log_failure("Marketing trial notification could not be recorded")
        return
    try:
        frappe.enqueue(
            "qas_custom.services.marketing_notifications.queue_delivery",
            notification=delivery.name, queue="short", enqueue_after_commit=True,
            job_id="marketing-trial-" + delivery.name, deduplicate=True,
        )
    except Exception:
        # The committed Pending record is recovered by the scheduler.
        _log_failure("Marketing trial notification worker unavailable")


def email_body(parent_name):
    return "<p>A new website trial enquiry has been received.</p><p>Parent name: {0}</p>".format(
        escape_html(str(parent_name or "Not provided"))
    )


def queue_delivery(notification):
    # Queue creation and the Queued marker commit together. Never send SMTP here.
    rows = frappe.db.sql("SELECT name FROM `tabMarketing Trial Notification` WHERE name=%s FOR UPDATE", (notification,))
    if not rows:
        return
    doc = frappe.get_doc(DELIVERY, notification, for_update=True)
    if doc.status not in {"Pending", "Failed"} or doc.email_queue:
        return
    # Locking reads avoid stale snapshots after another worker/configuration save.
    settings = frappe.get_doc(SETTINGS, SETTINGS, for_update=True)
    if not cint(settings.enabled) or settings.recipient != doc.recipient:
        doc.status, doc.last_error = "Skipped", "Notifications disabled or recipient changed before handoff."
        doc.save(ignore_permissions=True)
        return
    if not outbound_email_enabled():
        # Staging must not create mail for later accidental delivery.
        doc.status, doc.last_error = "Skipped", "Outbound email disabled for this environment."
        doc.save(ignore_permissions=True)
        return
    frappe.db.savepoint("marketing_trial_email")
    try:
        _, recipient = validate_settings(1, doc.recipient)
        queue = sendmail_or_skip(
            action="marketing_trial_new", recipients=[recipient], subject=SUBJECT,
            message=email_body(doc.parent_name), delayed=True, now=False,
            add_unsubscribe_link=0, is_notification=True,
        )
        if not getattr(queue, "name", None):
            raise RuntimeError("Email Queue was not created.")
        doc.status, doc.email_queue, doc.last_error = "Queued", queue.name, ""
        doc.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback(save_point="marketing_trial_email")
        doc = frappe.get_doc(DELIVERY, notification, for_update=True)
        doc.status, doc.last_error = "Failed", "Email queue handoff failed; automatic retry pending."
        doc.save(ignore_permissions=True)
        _log_failure("Marketing trial email queue handoff failed")


def retry_pending():
    if not scheduler_enabled():
        return
    for name in frappe.get_all(DELIVERY, filters={"status": ["in", ["Pending", "Failed"]]}, pluck="name", limit_page_length=100, order_by="modified asc"):
        try:
            queue_delivery(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            _log_failure("Marketing trial retry failed")


def _log_failure(title):
    try:
        frappe.log_error(title=title, message=frappe.get_traceback())
    except Exception:
        # Notification diagnostics must not turn a saved application into an error.
        pass
