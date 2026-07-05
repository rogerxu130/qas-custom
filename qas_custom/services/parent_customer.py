from __future__ import annotations

import frappe


def ensure_parent_customer_after_save(doc, method=None):
	return ensure_parent_customer(doc)


def ensure_parent_customer(parent):
	if not _doctype_available("Customer") or not _has_field("Parent", "customer"):
		return None

	doc = frappe.get_doc("Parent", parent) if isinstance(parent, str) else parent
	if not doc or not doc.get("name"):
		return None

	existing = doc.get("customer")
	if existing and frappe.db.exists("Customer", existing):
		return existing

	existing = _existing_customer_for_parent(doc)
	if existing:
		frappe.db.set_value("Parent", doc.name, "customer", existing, update_modified=False)
		doc.set("customer", existing)
		return existing

	customer_name = doc.get("parent_name") or doc.get("name")
	customer = frappe.new_doc("Customer")
	customer.customer_name = customer_name
	_set_if_field(customer, "customer_type", "Individual")
	_set_if_field(customer, "email_id", _parent_email(doc))
	_set_if_field(customer, "mobile_no", _parent_mobile(doc))
	_set_default_customer_fields(customer)
	customer.insert(ignore_permissions=True)

	frappe.db.set_value("Parent", doc.name, "customer", customer.name, update_modified=False)
	doc.set("customer", customer.name)
	return customer.name


def _set_default_customer_fields(customer):
	if customer.meta.has_field("customer_group") and not customer.get("customer_group"):
		customer_group = _first_existing("Customer Group", ["Individual", "All Customer Groups"])
		if customer_group:
			customer.customer_group = customer_group
	if customer.meta.has_field("territory") and not customer.get("territory"):
		territory = _first_existing("Territory", ["All Territories", "New Zealand", "Australia"])
		if territory:
			customer.territory = territory


def _parent_email(doc):
	for fieldname in ("email", "email_id", "contact_email", "linked_user"):
		value = (doc.get(fieldname) or "").strip()
		if value:
			return value
	return None


def _parent_mobile(doc):
	for fieldname in ("mobile_number", "mobile_no", "phone"):
		value = (doc.get(fieldname) or "").strip()
		if value:
			return value
	return None


def _existing_customer_for_parent(doc):
	email = _parent_email(doc)
	if not email:
		return None
	for fieldname in ("email_id", "email", "contact_email"):
		if _has_field("Customer", fieldname):
			customer = frappe.db.get_value("Customer", {fieldname: email}, "name")
			if customer:
				return customer
	return None


def _first_existing(doctype, preferred):
	if not _doctype_available(doctype):
		return None
	for value in preferred:
		if frappe.db.exists(doctype, value):
			return value
	rows = frappe.get_all(doctype, pluck="name", limit=1)
	return rows[0] if rows else None


def _set_if_field(doc, fieldname, value):
	if value is not None and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _has_field(doctype, fieldname):
	return _doctype_available(doctype) and frappe.db.has_column(doctype, fieldname)


def _doctype_available(doctype):
	return frappe.db.exists("DocType", doctype)
