from __future__ import annotations

import frappe


def clear_frappe_messages():
	if hasattr(frappe, "clear_messages"):
		frappe.clear_messages()
	elif hasattr(frappe.local, "message_log"):
		frappe.local.message_log = []


def has_field(doctype: str, fieldname: str):
	try:
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False


def is_new_doc(doc):
	if hasattr(doc, "is_new"):
		return doc.is_new()
	return bool(doc.get("__islocal"))


def set_if_field(doc, fieldname: str, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)
