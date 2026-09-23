"""Add PAYG invoice options and row provenance without changing legacy defaults."""
import frappe


def execute():
    for dt, fieldname, options in (
            ("Sales Invoice", "qas_invoice_type", ("PAYG Card",)),
            ("Sales Invoice Item", "qas_line_type", ("PAYG Card", "PAYG Exchange"))):
        _append_options(dt, fieldname, options)
        frappe.clear_cache(doctype=dt)
    _ensure_source_field("qas_source_doctype", "Link", "Source DocType", "DocType", "qas_line_type")
    _ensure_source_field("qas_source_document", "Data", "Source Document", None, "qas_source_doctype")
    frappe.clear_cache(doctype="Sales Invoice Item")


def _append_options(dt, fieldname, additions):
    # Property Setter overrides Custom Field at runtime, so update it as well
    # when one exists. Preserve every pre-existing option and the field default.
    for doctype, filters, column in (
            ("Custom Field", {"dt": dt, "fieldname": fieldname}, "options"),
            ("Property Setter", {"doc_type": dt, "field_name": fieldname,
                                 "property": "options"}, "value")):
        rows = frappe.get_all(doctype, filters=filters, fields=["name", column], limit=1)
        if not rows:
            continue
        row = rows[0]
        existing = (row.get(column) or "").splitlines()
        updated = existing + [option for option in additions if option not in existing]
        if updated != existing:
            frappe.db.set_value(doctype, row.name, column, "\n".join(updated), update_modified=False)


def _ensure_source_field(fieldname, fieldtype, label, options, insert_after):
    dt = "Sales Invoice Item"
    if frappe.db.exists("DocField", {"parent": dt, "fieldname": fieldname}):
        return
    if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
        return
    payload = {"doctype": "Custom Field", "dt": dt, "fieldname": fieldname,
               "fieldtype": fieldtype, "label": label, "insert_after": insert_after}
    if options:
        payload["options"] = options
    frappe.get_doc(payload).insert(ignore_permissions=True)
