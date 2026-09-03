from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, flt


ADMIN_ROLES = {"School Admin", "System Manager"}


def get_school_admin_invoice_discount_templates_data(include_inactive=0):
	_require_school_admin()
	filters = {} if cint(include_inactive) else {"status": "Active"}
	rows = frappe.get_all(
		"Invoice Discount Template",
		filters=filters,
		fields=["name", "template_name", "description", "discount_type", "discount_value", "status", "modified"],
		order_by="status asc, template_name asc",
		limit=500,
	)
	return {"items": [_template_payload(row) for row in rows]}


def save_school_admin_invoice_discount_template_data(discount_template=None, payload=None):
	_require_school_admin()
	payload = _payload(payload)
	doc = (
		frappe.get_doc("Invoice Discount Template", discount_template)
		if discount_template
		else frappe.new_doc("Invoice Discount Template")
	)
	for field in ("template_name", "description", "discount_type", "discount_value", "status"):
		if field in payload:
			doc.set(field, payload.get(field))
	if doc.is_new() and not doc.status:
		doc.status = "Active"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"template": _template_payload(doc)}


def _template_payload(row):
	return {
		"name": row.get("name"),
		"template_name": row.get("template_name"),
		"description": row.get("description"),
		"discount_type": row.get("discount_type"),
		"discount_value": flt(row.get("discount_value")),
		"status": row.get("status"),
		"modified": str(row.get("modified") or ""),
	}


def _payload(value):
	if value is None:
		value = frappe.form_dict.get("payload")
	if isinstance(value, str):
		return json.loads(value) if value.strip() else {}
	return dict(value or {})


def _require_school_admin():
	if frappe.session.user == "Guest" or not set(frappe.get_roles(frappe.session.user)).intersection(ADMIN_ROLES):
		frappe.throw(_("Only School Admin or System Manager users can manage Invoice Discount Templates."), frappe.PermissionError)
