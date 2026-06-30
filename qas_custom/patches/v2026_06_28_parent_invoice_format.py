from __future__ import annotations

import frappe


PRINT_FORMAT_NAME = "QAS Parent Invoice"


def execute():
	_upsert_parent_invoice_print_format()
	_set_default_sales_invoice_print_format()
	_disable_legacy_invoice_notifications()


def _upsert_parent_invoice_print_format():
	html = _parent_invoice_print_html()
	if frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		doc = frappe.get_doc("Print Format", PRINT_FORMAT_NAME)
		doc.doc_type = "Sales Invoice"
		doc.print_format_type = "Jinja"
		doc.custom_format = 1
		doc.html = html
		doc.disabled = 0
		doc.save(ignore_permissions=True)
		return

	doc = frappe.get_doc(
		{
			"doctype": "Print Format",
			"name": PRINT_FORMAT_NAME,
			"doc_type": "Sales Invoice",
			"print_format_type": "Jinja",
			"custom_format": 1,
			"standard": "No",
			"disabled": 0,
			"html": html,
		}
	)
	doc.insert(ignore_permissions=True)


def _set_default_sales_invoice_print_format():
	if not frappe.db.exists("Property Setter", {"doc_type": "Sales Invoice", "property": "default_print_format"}):
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": "Sales Invoice",
				"doctype_or_field": "DocType",
				"property": "default_print_format",
				"property_type": "Data",
				"value": PRINT_FORMAT_NAME,
			}
		).insert(ignore_permissions=True)
	else:
		for name in frappe.get_all(
			"Property Setter",
			filters={"doc_type": "Sales Invoice", "property": "default_print_format"},
			pluck="name",
		):
			frappe.db.set_value("Property Setter", name, "value", PRINT_FORMAT_NAME, update_modified=False)
	frappe.clear_cache(doctype="Sales Invoice")


def _disable_legacy_invoice_notifications():
	if not frappe.db.exists("DocType", "Notification"):
		return

	meta = frappe.get_meta("Notification")
	fields = ["name"]
	for fieldname in ["document_type", "subject", "message", "enabled"]:
		if meta.has_field(fieldname):
			fields.append(fieldname)
	rows = frappe.get_all("Notification", filters={"document_type": "Sales Invoice"}, fields=fields, limit_page_length=0)
	for row in rows:
		text = " ".join(str(row.get(fieldname) or "") for fieldname in ["subject", "message"])
		if _looks_like_legacy_invoice_notification(text):
			if meta.has_field("enabled"):
				frappe.db.set_value("Notification", row.name, "enabled", 0, update_modified=False)
			elif meta.has_field("disabled"):
				frappe.db.set_value("Notification", row.name, "disabled", 1, update_modified=False)


def _looks_like_legacy_invoice_notification(text: str) -> bool:
	needle_text = text.lower()
	return any(
		needle.lower() in needle_text
		for needle in [
			"queensland art school - new invoice",
			"a new invoice has been generated for your account",
			"please log in to your parent portal to view and pay",
		]
	)


def _parent_invoice_print_html():
	return """
{% set invoice_total = doc.grand_total or doc.rounded_total or 0 %}
{% set outstanding = doc.outstanding_amount if doc.outstanding_amount is not none else invoice_total %}
{% set qas_credit = doc.get("qas_store_credit_applied") %}
{% set qas_payable = doc.get("qas_amount_payable") %}
{% set credit_applied = qas_credit if qas_credit is not none else (invoice_total - outstanding if invoice_total > outstanding else 0) %}
{% set payable_amount = qas_payable if qas_payable is not none else (outstanding if outstanding > 0 else invoice_total - credit_applied) %}
<style>
	.qas-invoice {
		color: #172033;
		font-family: Arial, sans-serif;
		font-size: 12px;
		line-height: 1.45;
	}
	.qas-invoice * {
		box-sizing: border-box;
	}
	.qas-header {
		border-bottom: 2px solid #172033;
		border-collapse: collapse;
		margin-bottom: 22px;
		padding-bottom: 18px;
		width: 100%;
	}
	.qas-header td {
		vertical-align: top;
	}
	.qas-brand {
		color: #e85f47;
		font-size: 12px;
		font-weight: 700;
		letter-spacing: .04em;
		margin: 0 0 8px;
		text-transform: uppercase;
	}
	.qas-title {
		font-size: 28px;
		font-weight: 800;
		margin: 0;
	}
	.qas-muted {
		color: #64748b;
	}
	.qas-summary {
		background: #f8fafc;
		border: 1px solid #e5e7eb;
		border-radius: 12px;
		margin-bottom: 22px;
		width: 100%;
	}
	.qas-summary td {
		padding: 14px;
		width: 33.333%;
	}
	.qas-summary strong {
		display: block;
		font-size: 17px;
		margin-top: 4px;
	}
	.qas-payable {
		color: #e85f47;
	}
	.qas-table {
		border-collapse: collapse;
		margin-bottom: 18px;
		width: 100%;
	}
	.qas-table th {
		background: #f1f5f9;
		color: #64748b;
		font-size: 11px;
		padding: 10px;
		text-align: left;
		text-transform: uppercase;
	}
	.qas-table td {
		border-bottom: 1px solid #e5e7eb;
		padding: 10px;
		vertical-align: top;
	}
	.qas-table .right {
		text-align: right;
	}
	.qas-total {
		margin-left: auto;
		width: 280px;
	}
	.qas-total td {
		padding: 7px 0;
	}
	.qas-total .final td {
		border-top: 2px solid #172033;
		font-size: 16px;
		font-weight: 800;
		padding-top: 10px;
	}
	.qas-note {
		background: #fff7ed;
		border: 1px solid #fed7aa;
		border-radius: 10px;
		color: #7c2d12;
		margin-top: 24px;
		padding: 12px;
	}
	.qas-message {
		background: #f8fafc;
		border: 1px solid #e5e7eb;
		border-radius: 10px;
		margin-bottom: 18px;
		padding: 12px;
		white-space: pre-line;
	}
	.qas-bank {
		border-collapse: collapse;
		margin-top: 8px;
		width: 100%;
	}
	.qas-bank td {
		padding: 4px 0;
	}
</style>
<div class="qas-invoice">
	<table class="qas-header">
		<tr>
		<td>
			<p class="qas-brand">Queensland Art School</p>
			<h1 class="qas-title">Invoice</h1>
			<p class="qas-muted">{{ doc.name }}</p>
		</td>
		<td style="text-align:right;">
			<p><strong>Due date</strong><br>{{ doc.due_date or "-" }}</p>
			<p><strong>Invoice date</strong><br>{{ doc.posting_date or "-" }}</p>
		</td>
		</tr>
	</table>

	<table class="qas-summary">
		<tr>
			<td><span class="qas-muted">Invoice total</span><strong>AUD ${{ "%.2f"|format(invoice_total) }}</strong></td>
			<td><span class="qas-muted">Store credit applied</span><strong>AUD ${{ "%.2f"|format(credit_applied) }}</strong></td>
			<td><span class="qas-muted">Amount payable</span><strong class="qas-payable">AUD ${{ "%.2f"|format(payable_amount) }}</strong></td>
		</tr>
	</table>

	{% if doc.qas_invoice_message %}
	<div class="qas-message">{{ doc.qas_invoice_message }}</div>
	{% endif %}

	<table class="qas-table">
		<thead>
			<tr>
				<th style="width:20%;">Student</th>
				<th>Course</th>
				<th class="right" style="width:13%;">Sessions</th>
				<th class="right" style="width:16%;">Unit price</th>
				<th class="right" style="width:16%;">Amount</th>
			</tr>
		</thead>
		<tbody>
			{% for item in doc.items %}
			<tr>
				<td><strong>{{ item.student_display_name or item.student or "Student" }}</strong></td>
				<td>{{ item.description }}</td>
				<td class="right">{{ item.session_count or item.qty }}</td>
				<td class="right">AUD ${{ "%.2f"|format(item.rate) }}</td>
				<td class="right"><strong>AUD ${{ "%.2f"|format(item.amount) }}</strong></td>
			</tr>
			{% endfor %}
		</tbody>
	</table>

	<table class="qas-total">
		<tr><td>Invoice total</td><td style="text-align:right;"><strong>AUD ${{ "%.2f"|format(invoice_total) }}</strong></td></tr>
		<tr><td>Store credit applied</td><td style="text-align:right;"><strong>AUD ${{ "%.2f"|format(credit_applied) }}</strong></td></tr>
		<tr class="final"><td>Amount payable</td><td style="text-align:right;">AUD ${{ "%.2f"|format(payable_amount) }}</td></tr>
	</table>

	<div class="qas-note">
		<strong>Payment</strong><br>
		{% if payable_amount > 0 %}
		Please arrange payment by {{ doc.qas_accepted_payment_methods or "bank transfer, cash, or POS" }}. If you have already paid, no further action is needed.
		{% if doc.qas_bank_account_name or doc.qas_bank_bsb or doc.qas_bank_account_number %}
		<table class="qas-bank">
			{% if doc.qas_bank_account_name %}<tr><td>Account name</td><td style="text-align:right;"><strong>{{ doc.qas_bank_account_name }}</strong></td></tr>{% endif %}
			{% if doc.qas_bank_bsb %}<tr><td>BSB</td><td style="text-align:right;"><strong>{{ doc.qas_bank_bsb }}</strong></td></tr>{% endif %}
			{% if doc.qas_bank_account_number %}<tr><td>Account number</td><td style="text-align:right;"><strong>{{ doc.qas_bank_account_number }}</strong></td></tr>{% endif %}
		</table>
		{% endif %}
		{% if doc.qas_bank_reference_note %}<p style="margin:8px 0 0;">{{ doc.qas_bank_reference_note }}</p>{% endif %}
		{% else %}
		This invoice is fully covered by store credit. No payment is required.
		{% endif %}
	</div>
</div>
"""
