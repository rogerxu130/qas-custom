"""Welcome emails created atomically with a newly issued family class pass."""
import frappe
from frappe.utils import escape_html, getdate

from qas_custom.modules.notifications.commands import _create_notification_log, _mark_notification_queued
from qas_custom.modules.notifications.makeup_parent_notifications import _parent_portal_url, _parent_recipient
from qas_custom.utils.environment import sendmail_or_skip


def welcome_email_content(card, recipient):
    login_url = _parent_portal_url("/psugo-login")
    issued_on = getdate(card.issued_on).strftime("%d %B %Y")
    expires_on = getdate(card.expires_on).strftime("%d %B %Y")
    return f"""<h2>Your QAS 10-Class Pass is ready</h2>
<p>Hi {escape_html(recipient.get('parent_name') or 'Parent')},</p>
<p>Welcome to the QAS 10-Class Pass! Your family can now book classes using your pass.</p>
<p><strong>Pass:</strong> {escape_html(card.name)}<br>
<strong>Course:</strong> {escape_html(card.course)}<br>
<strong>Includes:</strong> 10 class visits, shared between siblings in your family<br>
<strong>Issued:</strong> {issued_on}<br>
<strong>Valid until:</strong> {expires_on} (Brisbane time)</p>
<h3>Log in and book</h3>
<p><a href="{escape_html(login_url)}">Log in to the QAS Pay-as-you-go Portal</a></p>
<p>Use your registered email address and password. To set or reset your password,
select &ldquo;Forgot password&rdquo; on the login page. If you cannot access your account, reply to this email for help.</p>
<p>Select your child, choose the course on your pass, and select an available class.
You can choose different campuses, teachers and class times for that course,
subject to availability and your child&rsquo;s age and level.</p>
<p>Book <strong>at least 72 hours before the class starts</strong>.
Each child attending one class uses one visit, so two children attending use two visits.
A confirmed booking reserves one visit immediately.</p>
<h3>Validity and cancellations</h3>
<p>Your pass is initially valid for six months from issue and can be used across school terms.
Both your booking and the class must fall within the pass&rsquo;s validity period.</p>
<p>You can cancel online <strong>more than 72 hours before the class starts</strong>.
The visit is returned to the original pass, with its original expiry date; no cash refund or makeup voucher is issued.
At exactly 72 hours before class, online cancellation is already closed.</p>
<p>Missed classes do not automatically receive a returned visit or a makeup voucher.
If something unexpected happens, please contact us so we can review your booking.
If QAS needs to cancel a class, we will contact you and return the visit to your pass.</p>
<h3>Renewing or changing courses</h3>
<p>Reply to this email to purchase another pass, request an extension or discuss changing courses.
Unused visits remain recorded after expiry but cannot be booked until an extension is arranged.
Course changes are arranged by our team, may involve a price difference, and do not automatically move existing bookings.</p>
<p>If you need any help, simply reply to this email.</p>
<p>Kind regards,<br>Queensland Art School</p>"""


def queue_welcome_notification(card):
    """The issuing command holds the purchase lock; queue transport runs after commit."""
    subject = f"Your QAS 10-Class Pass — {card.name}"
    savepoint = "payg_welcome_email"
    frappe.db.savepoint(savepoint)
    try:
        if frappe.db.exists("Notification Log", {
            "document_type": "QAS PAYG Card", "document_name": card.name, "subject": subject,
        }):
            return
        recipient = _parent_recipient(card.family_parent)
        if not recipient.get("email"):
            raise ValueError("No email address found for the pass family.")
        message = welcome_email_content(card, recipient)
        queue = sendmail_or_skip(
            action="payg_card_welcome", recipients=[recipient["email"]],
            subject=subject, message=message,
            reference_doctype="QAS PAYG Card", reference_name=card.name,
            now=False, delayed=True,
        )
        if isinstance(queue, dict) and queue.get("skipped"):
            return
        if not getattr(queue, "name", None):
            raise ValueError("Welcome email could not be queued.")
        log_name = _create_notification_log(
            f"payg_card_welcome:{card.name}", recipient, subject, message, "QAS PAYG Card", card.name,
        )
        if not log_name:
            raise ValueError("Welcome email notification record could not be created.")
        _mark_notification_queued(log_name)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.log_error(title="PAYG welcome email could not be queued", message=frappe.get_traceback())
