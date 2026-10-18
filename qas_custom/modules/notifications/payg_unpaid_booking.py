"""School Admin-only financial warning after a ten-class pass booking commits."""
from hashlib import sha256
from urllib.parse import quote

import frappe
from frappe.utils import escape_html, flt

from qas_custom.utils.environment import outbound_email_enabled, sendmail_or_skip


def queue_warning(booking):
    frappe.enqueue("qas_custom.modules.notifications.payg_unpaid_booking.send_warning", queue="short",
        enqueue_after_commit=True, job_id="payg-unpaid-" + booking, deduplicate=True, booking=booking)


def send_warning(booking):
    if not outbound_email_enabled():
        return {"skipped": True, "reason": "Outbound email is disabled."}
    row = frappe.db.get_value("QAS PAYG Booking", booking, ["card", "family_parent", "status"], as_dict=True)
    if not row or row.status == "Cancelled":
        return {"skipped": True}
    # Same lock as card issuance and booking serializes duplicate warning workers.
    frappe.db.sql("SELECT name FROM `tabQAS PAYG Card` WHERE name=%s FOR UPDATE", (row.card,))
    from qas_custom.services.payg_payment_followups import unpaid_booking_warnings
    from qas_custom.modules.notifications.commands import _payg_school_admin_emails
    from qas_custom.modules.notifications.makeup_parent_notifications import _parent_portal_url
    recipients = _payg_school_admin_emails()
    if not recipients:
        return {"skipped": True, "reason": "No enabled School Admin email is configured."}
    sent = []
    for invoice in unpaid_booking_warnings():
        if invoice["parent"] != row.family_parent or not any(item["name"] == booking for item in invoice["bookings"]):
            continue
        key = "payg-unpaid-" + sha256(f"{row.card}:{invoice['name']}".encode()).hexdigest()[:24]
        if frappe.db.exists("Notification Log", key):
            continue
        subject = f"Action required: ten-class pass booked with unpaid invoice {invoice['name']}"
        url = _parent_portal_url("/school-admin?tab=invoices&origin=notification&record=" + quote(invoice["name"], safe=""))
        message = f"""<h2>Ten-class pass payment warning</h2>
<p>{escape_html(invoice.get('customer_name') or invoice['parent'])} has an active ten-class pass booking while the linked invoice still has an outstanding balance.</p>
<p>Card: {escape_html(row.card)}<br>Invoice: {escape_html(invoice['name'])}<br>
Outstanding invoice balance: A${flt(invoice['outstanding_amount']):.2f}<br>
Payment due: {escape_html(str(invoice.get('due_date') or 'Not set'))}<br>
Active bookings linked to this invoice: {invoice['booking_count']}</p>
<p>The invoice may include other family charges. Please review the invoice before contacting the family.</p>
<p><a href="{escape_html(url)}">Review invoice</a></p>"""
        # A deterministic primary key also protects retries with an older transaction snapshot.
        # Insert only the marker: Notification Log.after_insert can send a second email.
        frappe.db.savepoint("payg_warning_marker")
        try:
            frappe.get_doc({"doctype": "Notification Log", "name": key, "type": "Alert", "subject": subject,
                "for_user": frappe.db.get_value("User", {"enabled": 1, "email": recipients[0]}, "name") or recipients[0], "document_type": "QAS PAYG Card", "document_name": row.card,
                "email_content": message}).db_insert()
        except frappe.DuplicateEntryError:
            frappe.db.rollback(save_point="payg_warning_marker")
            continue
        queue = sendmail_or_skip(action="payg_unpaid_booking_warning", recipients=recipients, subject=subject,
            message=message, reference_doctype="QAS PAYG Card", reference_name=row.card, now=False, delayed=True)
        if not getattr(queue, "name", None):
            raise ValueError("The unpaid booking warning could not be queued.")
        sent.append(invoice["name"])
    return {"queued": sent}
