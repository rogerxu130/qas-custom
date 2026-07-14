from __future__ import annotations

import frappe
from frappe.utils import cint


CONTACT_FIELDS = ["name", "teacher_name", "email", "mobile", "phone"]
SEARCH_FIELDS = ["name", "teacher_name", "email", "mobile", "phone"]


def get_active_teacher_directory_data(query=None, limit=300):
	if not _doctype_available("Teacher") or not _has_field("Teacher", "status"):
		return {"items": []}

	query = str(query or "").strip()
	limit = _limit(limit)
	fields = _safe_fields("Teacher", CONTACT_FIELDS)
	search_fields = [fieldname for fieldname in SEARCH_FIELDS if fieldname == "name" or _has_field("Teacher", fieldname)]
	kwargs = {
		"filters": {"status": "Active"},
		"fields": fields,
		"order_by": "teacher_name asc, name asc" if _has_field("Teacher", "teacher_name") else "name asc",
		"limit": limit,
	}
	if query and search_fields:
		kwargs["or_filters"] = [["Teacher", fieldname, "like", f"%{query}%"] for fieldname in search_fields]

	rows = frappe.get_all("Teacher", **kwargs)
	return {
		"items": [
			{
				"name": row.get("name"),
				"teacher_name": row.get("teacher_name") or row.get("name"),
				"email": row.get("email") or "",
				"mobile": row.get("mobile") or "",
				"phone": row.get("phone") or "",
			}
			for row in rows
		]
	}


def _limit(value, default=300, max_value=500):
	value = cint(value or default)
	if value <= 0:
		value = default
	return min(value, max_value)


def _safe_fields(doctype, candidates):
	return [fieldname for fieldname in candidates if fieldname == "name" or _has_field(doctype, fieldname)] or ["name"]


def _doctype_available(doctype):
	try:
		return bool(frappe.db.exists("DocType", doctype)) and bool(frappe.db.table_exists(doctype))
	except Exception:
		return False


def _has_field(doctype, fieldname):
	try:
		if fieldname == "name":
			return True
		return frappe.get_meta(doctype).has_field(fieldname)
	except Exception:
		return False
