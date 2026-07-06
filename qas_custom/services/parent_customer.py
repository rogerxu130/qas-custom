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
		_fill_customer_contact_if_blank(existing, doc)
		frappe.db.set_value("Parent", doc.name, "customer", existing, update_modified=False)
		doc.set("customer", existing)
		return existing

	customer_name = doc.get("parent_name") or doc.get("name")
	customer = frappe.new_doc("Customer")
	customer.customer_name = _available_customer_name(customer_name, _parent_email(doc))
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
	if email:
		for fieldname in ("email_id", "email", "contact_email"):
			if _has_field("Customer", fieldname):
				customer = frappe.db.get_value("Customer", {fieldname: email}, "name")
				if customer:
					return customer

	customer_name = (doc.get("parent_name") or doc.get("name") or "").strip()
	if not customer_name or not frappe.db.exists("Customer", customer_name):
		return None

	customer_email = _customer_email(customer_name)
	if not customer_email or (email and customer_email.lower() == email.lower()):
		return customer_name
	return None


def _available_customer_name(base_name, email=None):
	base_name = (base_name or email or "Parent Customer").strip()
	email = (email or "").strip()
	if not frappe.db.exists("Customer", base_name):
		return base_name

	if not email:
		return _dedupe_customer_name(base_name)

	return _dedupe_customer_name(base_name, email)


def _dedupe_customer_name(base_name, email=None):
	suffix = f" - {email}" if email else ""
	candidate = _fit_customer_name(base_name, suffix)
	if not frappe.db.exists("Customer", candidate):
		return candidate

	for index in range(2, 1000):
		numbered_suffix = f"{suffix} ({index})"
		candidate = _fit_customer_name(base_name, numbered_suffix)
		if not frappe.db.exists("Customer", candidate):
			return candidate

	return _fit_customer_name(base_name, f"{suffix} - {frappe.generate_hash(length=8)}")


def _fit_customer_name(base_name, suffix):
	max_length = 140
	base_name = (base_name or "Parent Customer").strip()
	suffix = suffix or ""
	if len(suffix) >= max_length:
		return suffix[-max_length:]
	if len(base_name) + len(suffix) <= max_length:
		return f"{base_name}{suffix}"
	return f"{base_name[: max_length - len(suffix)].rstrip()}{suffix}"


def _customer_email(customer):
	for fieldname in ("email_id", "email", "contact_email"):
		if _has_field("Customer", fieldname):
			value = (frappe.db.get_value("Customer", customer, fieldname) or "").strip()
			if value:
				return value
	return None


def _fill_customer_contact_if_blank(customer, parent_doc):
	updates = {}
	email = _parent_email(parent_doc)
	mobile = _parent_mobile(parent_doc)
	if email:
		for fieldname in ("email_id", "email", "contact_email"):
			if _has_field("Customer", fieldname) and not frappe.db.get_value("Customer", customer, fieldname):
				updates[fieldname] = email
				break
	if mobile and _has_field("Customer", "mobile_no") and not frappe.db.get_value("Customer", customer, "mobile_no"):
		updates["mobile_no"] = mobile
	if updates:
		frappe.db.set_value("Customer", customer, updates, update_modified=False)


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
