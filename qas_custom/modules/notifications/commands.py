from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, flt, now_datetime
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf

from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts
from qas_custom.modules.billing.presentation import build_parent_invoice_context, parent_portal_invoice_link


def send_parent_invoice_notification(
	invoice_doc,
	*,
	event: str = "approved",
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
	notification_log: str | None = None,
):
	amounts = _invoice_notification_amounts(invoice_doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	store_credit_applied = amounts["store_credit_applied"]
	payable_amount = amounts["payable_amount"]
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
			attachments=[
				_invoice_pdf_attachment(
					invoice_doc.name,
					store_credit_applied=store_credit_applied,
					payable_amount=payable_amount,
				)
			],
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
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
):
	amounts = _invoice_notification_amounts(invoice_doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	store_credit_applied = amounts["store_credit_applied"]
	payable_amount = amounts["payable_amount"]
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
	store_credit_applied: float | None = None,
	payable_amount: float | None = None,
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
	parent = invoice_doc.get("parent") or _parent_for_customer(invoice_doc.customer)
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

	if not email and invoice_doc.customer:
		email = _customer_contact_email(invoice_doc.customer)

	if not email and linked_user:
		email = frappe.db.get_value("User", linked_user, "email") or linked_user

	return {"email": email, "for_user": linked_user, "parent": parent, "customer": invoice_doc.customer}


def _parent_for_customer(customer):
	if not customer:
		return None
	if frappe.db.exists("DocType", "Parent") and frappe.db.has_column("Parent", "customer"):
		return frappe.db.get_value("Parent", {"customer": customer}, "name")
	return None


def _customer_contact_email(customer):
	if not customer or not frappe.db.exists("DocType", "Dynamic Link") or not frappe.db.exists("DocType", "Contact"):
		return None

	contacts = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		pluck="parent",
		limit_page_length=20,
	)
	contacts = [contact for contact in contacts if contact]
	if not contacts:
		return None

	fields = ["name"]
	for fieldname in ["email_id", "email", "contact_email", "is_primary_contact"]:
		if frappe.db.has_column("Contact", fieldname):
			fields.append(fieldname)
	filters = {"name": ["in", contacts]}
	if frappe.db.has_column("Contact", "disabled"):
		filters["disabled"] = 0
	order_by = "is_primary_contact desc, modified desc" if frappe.db.has_column("Contact", "is_primary_contact") else "modified desc"
	for row in frappe.get_all("Contact", filters=filters, fields=fields, order_by=order_by, limit_page_length=20):
		email = _first_value(row, ["email_id", "email", "contact_email"])
		if email:
			return email

	if not frappe.db.exists("DocType", "Contact Email") or not frappe.db.has_column("Contact Email", "email_id"):
		return None
	email_fields = ["parent", "email_id"]
	if frappe.db.has_column("Contact Email", "is_primary"):
		email_fields.append("is_primary")
	order_by = "is_primary desc, idx asc" if frappe.db.has_column("Contact Email", "is_primary") else "idx asc"
	for row in frappe.get_all(
		"Contact Email",
		filters={"parent": ["in", contacts]},
		fields=email_fields,
		order_by=order_by,
		limit_page_length=20,
	):
		if row.get("email_id"):
			return row.get("email_id")
	return None


def _invoice_notification_event_key(invoice_doc, event):
	base = f"invoice_{event}:{invoice_doc.name}"
	if event == "resent":
		return f"{base}:{now_datetime().strftime('%Y%m%d%H%M%S%f')}"
	return base


def _invoice_email_subject(invoice_doc, event):
	action = _("Invoice ready") if event == "approved" else _("Invoice resent")
	return _("Queensland Art School - {0} {1}").format(action, invoice_doc.name)


def render_parent_invoice_pdf(invoice: str, *, store_credit_applied=None, payable_amount=None):
	doc = frappe.get_doc("Sales Invoice", invoice)
	amounts = _invoice_notification_amounts(doc, store_credit_applied=store_credit_applied, payable_amount=payable_amount)
	context = build_parent_invoice_context(
		doc,
		store_credit_applied=amounts["store_credit_applied"],
		payable_amount=amounts["payable_amount"],
		invoice_link=parent_portal_invoice_link(doc.name),
	)
	html = _invoice_pdf_html(context)
	return get_pdf(html)


def _invoice_pdf_attachment(invoice: str, *, store_credit_applied=None, payable_amount=None):
	pdf_content = render_parent_invoice_pdf(
		invoice,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
	)
	file_doc = save_file(
		f"{invoice}.pdf",
		pdf_content,
		"Sales Invoice",
		invoice,
		is_private=1,
	)
	return {
		"fid": file_doc.name,
	}


def _invoice_notification_amounts(invoice_doc, *, store_credit_applied=None, payable_amount=None):
	return resolve_invoice_print_amounts(
		invoice_doc,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
	)


def _invoice_pdf_html(context):
	rows = "\n".join(_invoice_pdf_item_row(item) for item in context["items"])
	if not rows:
		rows = """<tr><td colspan="5" class="muted">Invoice details are available in the Parent Portal.</td></tr>"""

	payment_block = _invoice_pdf_payment_block(context)
	invoice_message = _invoice_pdf_message(context.get("invoice_message"))
	return """
<!doctype html>
<html>
<head>
	<meta charset="utf-8">
	<style>
		@page {{ size: A4; margin: 20mm 18mm; }}
		body {{ color: #172033; font-family: Arial, sans-serif; font-size: 12px; line-height: 1.45; }}
		* {{ box-sizing: border-box; }}
		.header {{ border-bottom: 2px solid #172033; margin-bottom: 22px; padding-bottom: 18px; width: 100%; }}
		.brand {{ color: #e85f47; font-size: 12px; font-weight: 700; letter-spacing: .04em; margin: 0 0 8px; text-transform: uppercase; }}
		h1 {{ font-size: 28px; font-weight: 800; margin: 0; }}
		.muted {{ color: #64748b; }}
		.summary {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; margin: 22px 0; width: 100%; }}
		.summary td {{ padding: 14px 12px; width: 33.333%; }}
		.summary span {{ color: #64748b; display: block; font-size: 12px; margin-bottom: 4px; }}
		.summary strong {{ display: block; font-size: 18px; }}
		.payable {{ color: #e85f47; }}
		.note {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; margin: 0 0 22px; padding: 14px 16px; }}
		table.items {{ border-collapse: collapse; margin-top: 18px; width: 100%; }}
		table.items th {{ background: #f1f5f9; color: #64748b; font-size: 11px; padding: 10px 8px; text-align: left; text-transform: uppercase; }}
		table.items td {{ border-bottom: 1px solid #e5e7eb; padding: 11px 8px; vertical-align: top; }}
		.totals {{ margin-left: auto; margin-top: 22px; width: 360px; }}
		.totals td {{ padding: 7px 0; }}
		.totals .final td {{ border-top: 2px solid #172033; font-size: 16px; font-weight: 800; padding-top: 12px; }}
		.payment {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 10px; margin-top: 26px; padding: 14px 16px; }}
		.payment strong {{ display: block; font-size: 14px; margin-bottom: 4px; }}
		.right {{ text-align: right; }}
	</style>
</head>
<body>
	<table class="header">
		<tr>
			<td>
				<p class="brand">Queensland Art School</p>
				<h1>Invoice</h1>
				<div class="muted">{invoice}</div>
			</td>
			<td class="right">
				<strong>Due date</strong><br>{due_date}<br><br>
				<strong>Invoice date</strong><br>{posting_date}
			</td>
		</tr>
	</table>

	<table class="summary">
		<tr>
			<td><span>Invoice total</span><strong>AUD ${total:.2f}</strong></td>
			<td><span>Store credit applied</span><strong>AUD ${credit:.2f}</strong></td>
			<td><span>Amount payable</span><strong class="payable">AUD ${payable:.2f}</strong></td>
		</tr>
	</table>

	{invoice_message}

	<table class="items">
		<thead>
			<tr>
				<th>Student</th>
				<th>Course</th>
				<th class="right">Sessions</th>
				<th class="right">Unit price</th>
				<th class="right">Amount</th>
			</tr>
		</thead>
		<tbody>{rows}</tbody>
	</table>

	<table class="totals">
		<tr><td>Invoice total</td><td class="right"><strong>AUD ${total:.2f}</strong></td></tr>
		<tr><td>Store credit applied</td><td class="right"><strong>AUD ${credit:.2f}</strong></td></tr>
		<tr class="final"><td>Amount payable</td><td class="right">AUD ${payable:.2f}</td></tr>
	</table>

	{payment_block}
</body>
</html>
	""".format(
		invoice=escape_html(context["invoice"]),
		due_date=escape_html(context["due_date"] or "-"),
		posting_date=escape_html(context["posting_date"] or "-"),
		total=flt(context["total"]),
		credit=flt(context["store_credit_applied"]),
		payable=flt(context["payable_amount"]),
		invoice_message=invoice_message,
		rows=rows,
		payment_block=payment_block,
	)


def _invoice_pdf_item_row(item):
	return """
		<tr>
			<td><strong>{student}</strong></td>
			<td>{description}</td>
			<td class="right">{sessions:g}</td>
			<td class="right">AUD ${rate:.2f}</td>
			<td class="right"><strong>AUD ${amount:.2f}</strong></td>
		</tr>
	""".format(
		student=escape_html(item.get("student") or ""),
		description=escape_html(item.get("description") or ""),
		sessions=flt(item.get("sessions")),
		rate=flt(item.get("rate")),
		amount=flt(item.get("amount")),
	)


def _invoice_pdf_message(value):
	if not value:
		return ""
	return """<div class="note">{0}</div>""".format(escape_html(value).replace("\n", "<br>"))


def _invoice_pdf_payment_block(context):
	if flt(context["payable_amount"]) <= 0:
		message = (
			"This invoice is fully covered by store credit. No payment is required."
			if flt(context["store_credit_applied"]) > 0
			else "This invoice has no amount payable. No payment is required."
		)
		return """<div class="payment"><strong>Payment</strong>{0}</div>""".format(escape_html(message))

	rows = []
	for label, fieldname in [
		("Account name", "bank_account_name"),
		("BSB", "bank_bsb"),
		("Account number", "bank_account_number"),
	]:
		value = context.get(fieldname)
		if value:
			rows.append(
				"""<tr><td>{0}</td><td class="right"><strong>{1}</strong></td></tr>""".format(
					escape_html(label),
					escape_html(value),
				)
			)
	reference_note = escape_html(context.get("bank_reference_note") or "")
	return """
		<div class="payment">
			<strong>Payment</strong>
			Please arrange payment by {methods}. If you have already paid, no further action is needed.
			<table style="margin-top:12px;width:100%;">{rows}</table>
			<div style="margin-top:10px;">{reference_note}</div>
		</div>
	""".format(
		methods=escape_html(context.get("accepted_payment_methods") or "Bank transfer, cash, or POS"),
		rows="".join(rows),
		reference_note=reference_note,
	)


def _invoice_email_message(invoice_doc, event, store_credit_applied, payable_amount, payment_link):
	context = build_parent_invoice_context(
		invoice_doc,
		store_credit_applied=store_credit_applied,
		payable_amount=payable_amount,
		payment_link=payment_link,
	)
	greeting = _invoice_email_greeting(context)
	intro = (
		_("Your invoice is ready in the Parent Portal.")
		if event == "approved"
		else _("We have resent this invoice for your reference.")
	)
	if flt(context["payable_amount"]) > 0:
		payment_line = _("Please arrange payment by {0}.").format(context["accepted_payment_methods"] or _("bank transfer, cash, or POS"))
	elif flt(context["store_credit_applied"]) > 0:
		payment_line = _("No payment is required because this invoice is fully covered by store credit.")
	else:
		payment_line = _("No payment is required for this invoice.")
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
						<p style="margin:0 0 14px;font-size:16px;line-height:1.5;">{greeting}</p>
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
		greeting=greeting,
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


def _invoice_email_greeting(context):
	recipient_name = context.get("recipient_name")
	if recipient_name:
		return _("Hi {0},").format(escape_html(recipient_name))
	return _("Hi,")


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
