from __future__ import annotations

import frappe

from qas_custom.patches.v2026_07_20_add_manual_invoice_store_credit_choice import _ensure_custom_field


FIELDNAME = "qas_is_manual_invoice"


def execute():
	if not frappe.db.exists("DocType", "Sales Invoice"):
		return
	_ensure_custom_field(
		"Sales Invoice",
		{
			"fieldname": FIELDNAME,
			"fieldtype": "Check",
			"label": "QAS Manual Invoice",
			"insert_after": "qas_apply_store_credit_on_submit",
			"default": "0",
			"hidden": 1,
			"read_only": 1,
		},
	)
	frappe.clear_cache(doctype="Sales Invoice")
	_backfill_manual_invoices()


def _backfill_manual_invoices():
	required_fields = ["qas_is_manual_invoice", "qas_invoice_type", "source_doctype", "source_document", "enrollment"]
	if not all(frappe.db.has_column("Sales Invoice", fieldname) for fieldname in required_fields):
		return
	frappe.db.sql(
		"""
		UPDATE `tabSales Invoice`
		SET qas_is_manual_invoice = 1
		WHERE COALESCE(qas_is_manual_invoice, 0) = 0
		  AND COALESCE(qas_invoice_type, '') = 'Other'
		  AND COALESCE(source_doctype, '') = ''
		  AND COALESCE(source_document, '') = ''
		  AND COALESCE(enrollment, '') = ''
		"""
	)
