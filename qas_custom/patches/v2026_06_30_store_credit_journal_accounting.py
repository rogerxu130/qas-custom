from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from qas_custom.patches.v2026_06_28_parent_invoice_format import PRINT_FORMAT_NAME, _parent_invoice_print_html


def execute():
	_add_sales_invoice_snapshot_fields()
	_add_journal_entry_tracking_fields()
	_update_parent_invoice_print_format()
	_backfill_store_credit_accounting()
	frappe.clear_cache()


def _add_sales_invoice_snapshot_fields():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_store_credit_applied",
			"fieldtype": "Currency",
			"label": "QAS Store Credit Applied",
			"insert_after": _existing_field("Sales Invoice", ["outstanding_amount", "grand_total", "rounded_total"]),
			"read_only": 1,
		},
	)
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": "qas_amount_payable",
			"fieldtype": "Currency",
			"label": "QAS Amount Payable",
			"insert_after": "qas_store_credit_applied",
			"read_only": 1,
		},
	)


def _add_journal_entry_tracking_fields():
	if not frappe.db.exists("DocType", "Journal Entry"):
		return
	_ensure_custom_field(
		"Journal Entry",
		{
			"fieldname": "qas_store_credit_section",
			"fieldtype": "Section Break",
			"label": "QAS Store Credit",
			"insert_after": _existing_field("Journal Entry", ["user_remark", "remark", "cheque_date"]),
			"collapsible": 1,
		},
	)
	_ensure_custom_field(
		"Journal Entry",
		{
			"fieldname": "qas_store_credit_invoice",
			"fieldtype": "Link",
			"label": "QAS Store Credit Invoice",
			"options": "Sales Invoice",
			"insert_after": "qas_store_credit_section",
			"read_only": 1,
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Journal Entry",
		{
			"fieldname": "qas_store_credit_ledger",
			"fieldtype": "Link",
			"label": "QAS Store Credit Ledger",
			"options": "QAS Store Credit Ledger",
			"insert_after": "qas_store_credit_invoice",
			"read_only": 1,
			"in_standard_filter": 1,
		},
	)
	_ensure_custom_field(
		"Journal Entry",
		{
			"fieldname": "qas_store_credit_amount",
			"fieldtype": "Currency",
			"label": "QAS Store Credit Amount",
			"insert_after": "qas_store_credit_ledger",
			"read_only": 1,
		},
	)


def _update_parent_invoice_print_format():
	if not frappe.db.exists("Print Format", PRINT_FORMAT_NAME):
		return
	frappe.db.set_value(
		"Print Format",
		PRINT_FORMAT_NAME,
		"html",
		_parent_invoice_print_html(),
		update_modified=False,
	)


def _backfill_store_credit_accounting():
	if not frappe.db.table_exists("QAS Store Credit Ledger") or not frappe.db.exists("DocType", "Sales Invoice"):
		return

	from qas_custom.modules.billing.store_credit import (
		ensure_store_credit_journal_entry,
		sync_invoice_store_credit_snapshot,
	)

	rows = frappe.get_all(
		"QAS Store Credit Ledger",
		filters={"transaction_type": "Invoice Application", "debit_amount": [">", 0]},
		fields=["name", "invoice", "debit_amount"],
		order_by="creation asc",
		limit_page_length=0,
	)
	for row in rows:
		invoice = row.get("invoice")
		if not invoice or not frappe.db.exists("Sales Invoice", invoice):
			continue
		try:
			doc = frappe.get_doc("Sales Invoice", invoice)
			if doc.docstatus == 1:
				journal_entry = ensure_store_credit_journal_entry(doc, flt(row.get("debit_amount")), ledger=row.get("name"))
				if journal_entry and frappe.db.has_column("QAS Store Credit Ledger", "journal_entry"):
					frappe.db.set_value("QAS Store Credit Ledger", row.get("name"), "journal_entry", journal_entry, update_modified=False)
			sync_invoice_store_credit_snapshot(invoice)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				_("QAS store credit accounting backfill failed for {0}").format(invoice),
			)


def _ensure_custom_field(dt, values):
	fieldname = values["fieldname"]
	if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
		return

	name = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
	if name:
		doc = frappe.get_doc("Custom Field", name)
		changed = False
		for key, value in values.items():
			if doc.get(key) != value:
				doc.set(key, value)
				changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return

	frappe.get_doc({"doctype": "Custom Field", "dt": dt, **values}).insert(ignore_permissions=True)


def _existing_field(dt, fieldnames):
	for fieldname in fieldnames:
		if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}) or frappe.db.exists(
			"Custom Field", {"dt": dt, "fieldname": fieldname}
		):
			return fieldname
	return None
