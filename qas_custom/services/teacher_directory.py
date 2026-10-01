from __future__ import annotations

import frappe
from frappe.utils import cint, validate_email_address


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


def get_teacher_email_export_data(scope="active"):
	"""Complete, deduplicated Teacher.email list for School Admin export."""
	if scope not in ("active", "all"):
		frappe.throw(frappe._("Choose Active teachers or All teachers."))
	if not _doctype_available("Teacher") or not _has_field("Teacher", "email"):
		frappe.throw(frappe._("Teacher email records are unavailable."))
	if scope == "active" and not _has_field("Teacher", "status"):
		frappe.throw(frappe._("Teacher status is unavailable."))
	rows = frappe.get_all(
		"Teacher",
		filters={"status": "Active"} if scope == "active" else {},
		fields=_safe_fields("Teacher", ["name", "teacher_name", "email"]),
		order_by="teacher_name asc, name asc" if _has_field("Teacher", "teacher_name") else "name asc",
		limit_page_length=0,
	)
	items, seen = [], set()
	missing = invalid = duplicates = 0
	for row in rows:
		email = (row.get("email") or "").strip().lower()
		if not email:
			missing += 1
			continue
		# One plain mailbox per Teacher: never accept lists or display-name syntax.
		if any(char.isspace() or char in ",;<>" for char in email) or validate_email_address(email) != email:
			invalid += 1
			continue
		if email in seen:
			duplicates += 1
			continue
		seen.add(email)
		items.append({"teacher_name": row.get("teacher_name") or row["name"], "email": email})
	return {"scope": scope, "items": items, "teacher_count": len(rows), "email_count": len(items),
		"missing_count": missing, "invalid_count": invalid, "duplicate_count": duplicates}
