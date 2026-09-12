"""Transactional pickup emails; the Email Queue owns transport and retry state."""
from urllib.parse import quote

import frappe
from frappe.utils import escape_html, now_datetime

from qas_custom.modules.notifications.makeup_parent_notifications import _parent_portal_url, _parent_recipient
from qas_custom.utils.environment import email_block_reason, outbound_email_enabled, sendmail_or_skip


def notification_status(doc):
	queue_name = doc.get("ready_email_queue")
	if queue_name:
		queue = frappe.db.get_value("Email Queue", queue_name, ["status", "error"], as_dict=True)
		if queue:
			status = {"Sent": "Sent", "Error": "Failed", "Not Sent": "Queued", "Sending": "Queued", "Partially Sent": "Queued"}.get(queue.status, "Failed")
			return {"status": status, "error": "Email delivery failed. Please retry or contact the family." if status == "Failed" else ""}
		return {"status": "History expired", "error": "This email was queued, but its delivery history is no longer available."}
	if doc.get("ready_notification_skipped"):
		return {"status": "Skipped", "error": doc.get("ready_notification_error") or "Email is disabled in this environment."}
	return {"status": "Failed" if doc.get("ready_notification_error") else "Not sent", "error": doc.get("ready_notification_error") or ""}


def ready_email_content(doc, recipient):
	address = frappe.db.get_value("Campus", doc.pickup_campus, "address") or ""
	url = _parent_portal_url("/shop?order=" + quote(doc.name, safe=""))
	return f"""<h2>Your order is ready for collection</h2>
<p>Hello {escape_html(recipient.get('parent_name') or 'Parent')},</p>
<p>Your art materials are ready. Please quote order number <strong>{escape_html(doc.name)}</strong> at reception.</p>
<p><strong>Pickup campus:</strong> {escape_html(doc.pickup_campus)}<br>{escape_html(address)}</p>
<p>Collect during normal opening hours. Please pay for your materials at reception before collecting your order.</p>
<p><a href="{escape_html(url)}">View your order</a></p>"""


def queue_ready_notification(doc, retry=False):
	"""Called after saving Ready, while holding the order row lock, before request commit.

	Email Queue insertion participates in the same transaction as the status. A
	savepoint allows preparation failures without undoing the physical ready state.
	"""
	current = notification_status(doc)
	if current["status"] in {"Sent", "Queued", "History expired"}:
		return
	if current["status"] in {"Failed", "Skipped"} and not retry:
		return
	if not outbound_email_enabled():
		doc.db_set({"ready_notification_skipped": 1, "ready_notification_error": email_block_reason()}, update_modified=False)
		return
	frappe.db.savepoint("store_ready_email")
	try:
		if doc.get("ready_email_queue") and frappe.db.exists("Email Queue", doc.ready_email_queue):
			queue = frappe.get_doc("Email Queue", doc.ready_email_queue)
			if queue.status == "Error":
				queue.retry_sending()
			else:
				raise ValueError("The existing notification cannot be retried.")
		else:
			recipient = _parent_recipient(doc.parent)
			if not recipient.get("email"):
				raise ValueError("No parent email found. Update the family email and retry.")
			queue = sendmail_or_skip(
				action="store_order_ready", recipients=[recipient["email"]],
				subject=f"Order {doc.name} is ready for collection",
				message=ready_email_content(doc, recipient),
				reference_doctype="Store Order", reference_name=doc.name,
				now=False, delayed=True,
			)
			if not getattr(queue, "name", None):
				raise ValueError("The pickup email could not be queued. Please retry.")
		doc.db_set({"ready_email_queue": queue.name, "ready_email_queued_at": now_datetime(), "ready_notification_error": "", "ready_notification_skipped": 0}, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point="store_ready_email")
		frappe.log_error(title="Store order ready email failed", message=frappe.get_traceback())
		doc.db_set({"ready_notification_error": "The pickup email could not be queued. Check the family email and email settings, then retry.", "ready_notification_skipped": 0}, update_modified=False)
