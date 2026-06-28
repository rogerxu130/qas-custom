from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, flt, now_datetime

from qas_custom.modules.billing.presentation import build_parent_invoice_context, parent_portal_invoice_link

PARENT_INVOICE_PRINT_FORMAT = "QAS Parent Invoice"


def send_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
	notification_log: str | None = None,
):
	recipient = _invoice_recipient(invoice_doc)
	event_key = _invoice_notification_event_key(invoice_doc, event)
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _invoice_email_subject(invoice_doc, event)
	message = _invoice_email_message(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)

	log_name = notification_log or _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}

	try:
		frappe.sendmail(
			recipients=[recipient["email"]],
			subject=subject,
			message=message,
			reference_doctype="Sales Invoice",
			reference_name=invoice_doc.name,
			attachments=[_invoice_pdf_attachment(invoice_doc.name)],
		)
		_mark_notification_sent(log_name)
		return {
			"sent": True,
			"recipient": recipient["email"],
			"notification_log": log_name,
			"payment_link": payment_link,
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"QAS invoice notification failed: {invoice_doc.name}")
		_mark_notification_failed(log_name, "Email send failed.")
		_add_invoice_comment(invoice_doc.name, _("Invoice notification failed for {0}.").format(recipient["email"]))
		return {
			"sent": False,
			"recipient": recipient["email"],
			"reason": "Email send failed.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}


def enqueue_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
):
	recipient = _invoice_recipient(invoice_doc)
	event_key = _invoice_notification_event_key(invoice_doc, event)
	payment_link = parent_portal_invoice_link(invoice_doc.name)
	subject = _invoice_email_subject(invoice_doc, event)
	message = _invoice_email_message(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)
	log_name = _create_notification_log(
		event_key=event_key,
		recipient=recipient,
		subject=subject,
		message=message,
		document_type="Sales Invoice",
		document_name=invoice_doc.name,
	)

	if not recipient.get("email"):
		_mark_notification_failed(log_name, "No parent email found.")
		return {
			"sent": False,
			"queued": False,
			"reason": "No parent email found.",
			"notification_log": log_name,
			"payment_link": payment_link,
		}

	_mark_notification_queued(log_name)
	frappe.enqueue(
		"qas_custom.modules.notifications.commands.send_parent_invoice_notification_job",
		queue="short",
		timeout=300,
		enqueue_after_commit=True,
		invoice=invoice_doc.name,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		notification_log=log_name,
	)
	return {
		"sent": False,
		"queued": True,
		"recipient": recipient["email"],
		"notification_log": log_name,
		"payment_link": payment_link,
	}


def send_parent_invoice_notification_job(
	invoice: str,
	*,
	event: str = "approved",
	store_credit_applied: float = 0,
	payable_amount: float = 0,
	notification_log: str | None = None,
):
	invoice_doc = frappe.get_doc("Sales Invoice", invoice)
	return send_parent_invoice_notification(
		invoice_doc,
		event=event,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		notification_log=notification_log,
	)


def get_invoice_notification_summary(invoice: str):
	if not _notification_log_available() or not invoice:
		return {"count": 0, "latest": None}

	rows = frappe.get_all(
		"Notification Log",
		filters={"document_type": "Sales Invoice", "document_name": invoice},
		fields=_safe_notification_fields(),
		order_by="creation desc",
		limit=5,
	)
	return {
		"count": len(rows),
		"latest": _notification_payload(rows[0]) if rows else None,
		"items": [_notification_payload(row) for row in rows],
	}


def _invoice_recipient(invoice_doc):
	parent = invoice_doc.get("parent")
	linked_user = None
	email = None

	if parent:
		fields = ["name"]
		for fieldname in ["linked_user", "email", "email_id", "contact_email"]:
			if frappe.db.has_column("Parent", fieldname):
				fields.append(fieldname)
		parent_info = frappe.db.get_value("Parent", parent, fields, as_dict=True) or {}
		linked_user = parent_info.get("linked_user")
		email = _first_value(parent_info, ["email", "email_id", "contact_email"])

	if not email:
		for fieldname in ["contact_email", "email", "email_id"]:
			if frappe.db.has_column("Sales Invoice", fieldname) and invoice_doc.get(fieldname):
				email = invoice_doc.get(fieldname)
				break

	if not email and invoice_doc.customer:
		fields = []
		for fieldname in ["email_id", "email", "contact_email"]:
			if frappe.db.has_column("Customer", fieldname):
				fields.append(fieldname)
		if fields:
			customer_info = frappe.db.get_value("Customer", invoice_doc.customer, fields, as_dict=True) or {}
			email = _first_value(customer_info, fields)

	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user

	return {"email": email, "for_user": linked_user, "parent": parent, "customer": invoice_doc.customer}


def _invoice_notification_event_key(invoice_doc, event):
	base = f"invoice_{event}:{invoice_doc.name}"
	if event == "resent":
		return f"{base}:{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	return base


def _invoice_email_subject(invoice_doc, event):
	action = _("Invoice ready") if event == "approved" else _("Invoice resent")
	return _("Queensland Art School - {0} {1}").format(action, invoice_doc.name)


def _invoice_pdf_attachment(invoice: str):
	print_format = PARENT_INVOICE_PRINT_FORMAT if frappe.db.exists("Print Format", PARENT_INVOICE_PRINT_FORMAT) else None
	return frappe.attach_print(
		"Sales Invoice",
		invoice,
		file_name=invoice,
		print_format=print_format,
	)


def _invoice_email_message(invoice_doc, event, store_credit_applied, payable_amount, payment_link):
	context = build_parent_invoice_context(
		invoice_doc,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)
	intro = (
		_("Your invoice is ready in the Parent Portal.")
		if event == "approved"
		else _("We have resent this invoice for your reference.")
	)
	payment_line = (
		_("No payment is required because this invoice is fully covered by store credit.")
		if flt(context["payable_amount"]) <= 0
		else _("Please arrange payment by {0}.").format(context["accepted_payment_methods"] or _("bank transfer, cash, or POS"))
	)
	invoice_message = _html_multiline(context.get("invoice_message"))
	bank_details = _invoice_email_bank_details(context) if flt(context["payable_amount"]) > 0 else ""
	rows = "\n".join(_invoice_email_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="4" style="padding:12px;color:#64748b;">Invoice details are available in the Parent Portal.</td></tr>"""

	return """
		<div style="margin:0;padding:0;background:#f8fafc;font-family:Arial,sans-serif;color:#172033;">
			<div style="max-width:640px;margin:0 auto;padding:24px;">
				<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;">
					<div style="padding:22px 24px;background:#172033;color:#ffffff;">
						<p style="margin:0 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:#f7b6a4;">Queensland Art School</p>
						<h1 style="margin:0;font-size:24px;line-height:1.3;">Invoice {invoice}</h1>
					</div>
					<div style="padding:24px;">
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">Hi,</p>
						<p style="margin:0 0 18px;font-size:16px;line-height:1.5;">{intro}</p>
						{invoice_message}

						<table style="width:100%;border-collapse:collapse;margin:0 0 18px;">
							<tr>
								<td style="padding:10px 0;color:#64748b;">Due date</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">{due_date}</td>
							</tr>
							<tr>
								<td style="padding:10px 0;color:#64748b;">Invoice total</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${total:.2f}</td>
							</tr>
							<tr>
								<td style="padding:10px 0;color:#64748b;">Store credit applied</td>
								<td style="padding:10px 0;text-align:right;font-weight:700;">AUD ${credit:.2f}</td>
							</tr>
							<tr>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;font-size:17px;font-weight:700;">Amount payable</td>
								<td style="padding:12px 0;border-top:1px solid #e5e7eb;text-align:right;font-size:20px;font-weight:800;color:#e85f47;">AUD ${payable:.2f}</td>
							</tr>
						</table>

						<table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;margin:0 0 22px;">
							<thead>
								<tr style="background:#f1f5f9;">
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Student</th>
									<th align="left" style="padding:10px;font-size:12px;color:#64748b;">Course</th>
									<th align="right" style="padding:10px;font-size:12px;color:#64748b;">Sessions</th>
									<th align="right" style="padding:10px;font-size:12px;color:#64748b;">Amount</th>
								</tr>
							</thead>
							<tbody>{rows}</tbody>
						</table>

						<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;">{payment_line}</p>
						{bank_details}
						<p style="margin:0 0 22px;">
							<a href="{invoice_link}" style="display:inline-block;background:#e85f47;color:#ffffff;text-decoration:none;border-radius:10px;padding:12px 18px;font-weight:700;">View invoice</a>
						</p>
						<p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">If you have already paid, no further action is needed.</p>
					</div>
				</div>
			</div>
		</div>
	""".format(
		invoice=context["invoice"],
		intro=intro,
		invoice_message=invoice_message,
		due_date=context["due_date"] or "-",
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		payable=flt(context["payable_amount"]),
		rows=rows,
		payment_line=payment_line,
		bank_details=bank_details,
		invoice_link=context["invoice_link"],
	)


def _html_multiline(value):
	if not value:
		return ""
	return """<p style="margin:0 0 18px;font-size:15px;line-height:1.5;color:#334155;">{0}</p>""".format(
		escape_html(value).replace("\n", "<br>")
	)


def _invoice_email_bank_details(context):
	rows = []
	for label, fieldname in [
		(_("Account name"), "bank_account_name"),
		(_("BSB"), "bank_bsb"),
		(_("Account number"), "bank_account_number"),
	]:
		value = context.get(fieldname)
		if value:
			rows.append(
				"""<tr><td style="padding:6px 0;color:#64748b;">{0}</td><td style="padding:6px 0;text-align:right;font-weight:700;">{1}</td></tr>""".format(
					escape_html(label),
					escape_html(value),
				)
			)
	if not rows and not context.get("bank_reference_note"):
		return ""

	reference_note = _html_multiline(context.get("bank_reference_note"))
	return """
		<div style="margin:0 0 20px;padding:14px;border:1px solid #fed7aa;border-radius:10px;background:#fff7ed;">
			<p style="margin:0 0 8px;font-weight:700;color:#7c2d12;">Bank transfer details</p>
			<table style="width:100%;border-collapse:collapse;">{rows}</table>
			{reference_note}
		</div>
	""".format(rows="".join(rows), reference_note=reference_note)


def _invoice_email_item_row(item):
	return """
		<tr>
			<td style="padding:10px;border-top:1px solid #e5e7eb;font-weight:700;">{student}</td>
			<td style="padding:10px;border-top:1px solid #e5e7eb;">{description}</td>
			<td style="padding:10px;border-top:1px solid #e5e7eb;text-align:right;">{sessions:g}</td>
			<td style="padding:10px;border-top:1px solid #e5e7eb;text-align:right;font-weight:700;">AUD ${amount:.2f}</td>
		</tr>
	""".format(
		student=escape_html(item.get("student") or ""),
		description=escape_html(item.get("description") or ""),
		sessions=flt(item.get("sessions")),
		amount=flt(item.get("amount")),
	)


def _create_notification_log(event_key, recipient, subject, message, document_type, document_name):
	if not _notification_log_available():
		return None

	log = frappe.new_doc("Notification Log")
	log.subject = subject
	log.type = "Alert"
	log.email_content = message
	log.document_type = document_type
	log.document_name = document_name
	log.from_user = frappe.session.user
	if log.meta.has_field("for_user") and recipient.get("for_user"):
		log.for_user = recipient.get("for_user")
	elif log.meta.has_field("for_user"):
		log.for_user = frappe.session.user
	_set_if_field(log, "event_key", event_key)
	_set_if_field(log, "email_to", recipient.get("email"))
	_set_if_field(log, "recipient_email", recipient.get("email"))
	_set_if_field(log, "reference_doctype", document_type)
	_set_if_field(log, "reference_name", document_name)
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	return log.name


def _mark_notification_sent(log_name):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Sent"
	if meta.has_field("sent_at"):
		values["sent_at"] = now_datetime()
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _mark_notification_queued(log_name):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Queued"
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _mark_notification_failed(log_name, reason):
	if not log_name:
		return
	values = {}
	meta = frappe.get_meta("Notification Log")
	for fieldname in ["status", "delivery_status", "email_status"]:
		if meta.has_field(fieldname):
			values[fieldname] = "Failed"
	for fieldname in ["failure_reason", "error", "error_message"]:
		if meta.has_field(fieldname):
			values[fieldname] = reason
			break
	if values:
		frappe.db.set_value("Notification Log", log_name, values, update_modified=False)


def _notification_log_available():
	return bool(frappe.db.exists("DocType", "Notification Log"))


def _safe_notification_fields():
	meta = frappe.get_meta("Notification Log")
	fields = ["name", "subject", "creation", "document_type", "document_name"]
	for fieldname in ["for_user", "email_to", "recipient_email", "status", "delivery_status", "email_status", "failure_reason", "error_message"]:
		if meta.has_field(fieldname):
			fields.append(fieldname)
	return fields


def _notification_payload(row):
	status = row.get("delivery_status") or row.get("email_status") or row.get("status") or "Logged"
	return {
		"name": row.get("name"),
		"subject": row.get("subject"),
		"creation": row.get("creation"),
		"recipient": row.get("email_to") or row.get("recipient_email") or row.get("for_user"),
		"status": status,
		"failure_reason": row.get("failure_reason") or row.get("error_message"),
	}


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _first_value(mapping, fieldnames):
	for fieldname in fieldnames:
		value = mapping.get(fieldname)
		if value:
			return value
	return None


def _add_invoice_comment(invoice, message):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
			"content": message,
		}
	).insert(ignore_permissions=True)
