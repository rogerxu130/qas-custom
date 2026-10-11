"""Read-only record activity for the School Admin workspace."""
import json
from html import unescape

import frappe
from frappe.utils import cint, strip_html_tags

SUPPORTED_DOCTYPES = {"Enrollment", "Sales Invoice", "Inquiry", "Course Sessions", "Parent"}
PAGE_SIZE = 20


def _plain(value):
    if isinstance(value, str):
        return unescape(strip_html_tags(value))
    return value


def _changes(data, meta):
    result = []
    for fieldname, before, after in data.get("changed", []):
        field = meta.get_field(fieldname)
        if not field or field.fieldtype == "Password":
            continue
        result.append({"label": field.label or fieldname, "before": _plain(before), "after": _plain(after)})
    for kind, verb in (("added", "added"), ("removed", "removed")):
        for row in data.get(kind, []):
            field = meta.get_field(row[0])
            if field and field.fieldtype != "Password":
                result.append({"label": field.label or row[0], "before": None, "after": "Row " + verb})
    for fieldname, index, rowname, changed in data.get("row_changed", []):
        field = meta.get_field(fieldname)
        if field and field.fieldtype == "Table":
            child_changes = _changes({"changed": changed}, frappe.get_meta(field.options))
            for change in child_changes:
                change["label"] = f"{field.label or fieldname} · row {index + 1} · {change['label']}"
            result.extend(child_changes)
    return result


def get_activity(doctype=None, name=None, offset=0):
    # Use the same access rules as all other School Admin detail endpoints.
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    if doctype not in SUPPORTED_DOCTYPES or not isinstance(name, str) or not name.strip():
        frappe.throw("Unsupported activity record.", frappe.ValidationError)
    offset = cint(offset)
    if offset < 0 or offset > 10000:
        frappe.throw("Invalid activity offset.", frappe.ValidationError)
    doc = frappe.get_doc(doctype, name)
    meta = doc.meta
    # Fetch each source's prefix so pagination remains correct after merging.
    fetch_limit = offset + PAGE_SIZE + 1
    versions = frappe.get_all("Version", filters={"ref_doctype": doctype, "docname": name},
        fields=["name", "creation", "owner", "data"], order_by="creation desc, name desc", limit_page_length=fetch_limit)
    comments = frappe.get_all("Comment", filters={"reference_doctype": doctype, "reference_name": name},
        fields=["name", "creation", "owner", "comment_by", "comment_type", "content"],
        order_by="creation desc, name desc", limit_page_length=fetch_limit)
    items = []
    for row in versions:
        changes = _changes(json.loads(row.data or "{}"), meta)
        # Retain the event even when its values are protected, for stable pagination.
        items.append({"id": "version:" + row.name, "timestamp": str(row.creation), "actor": row.owner,
            "message": "Changed this record." if changes else "Updated this record.", "changes": changes})
    for row in comments:
        items.append({"id": "comment:" + row.name, "timestamp": str(row.creation),
            "actor": row.comment_by or row.owner, "message": _plain(row.content) or row.comment_type, "changes": []})
    items.append({"id": "created:" + name, "timestamp": str(doc.creation), "actor": doc.owner,
        "message": "Created this record.", "changes": []})
    if str(doc.modified) != str(doc.creation) and not any(str(row.creation) == str(doc.modified) for row in versions):
        items.append({"id": "edited:" + name, "timestamp": str(doc.modified), "actor": doc.modified_by,
            "message": "Last edited this record.", "changes": []})
    items.sort(key=lambda item: (item["timestamp"], item["id"]), reverse=True)
    page = items[offset:offset + PAGE_SIZE]
    actors = list({item["actor"] for item in page if item["actor"]})
    users = frappe.get_all("User", filters={"name": ["in", actors]}, fields=["name", "full_name"], limit_page_length=len(actors)) if actors else []
    names = {user.name: user.full_name for user in users}
    for item in page:
        item["actor"] = names.get(item["actor"]) or item["actor"] or "Unknown"
    return {"items": page, "has_more": len(items) > offset + PAGE_SIZE, "track_changes": bool(meta.track_changes)}
