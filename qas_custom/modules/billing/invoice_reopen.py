"""Same-number reopen after normal ERPNext accounting cancellation succeeds."""
import frappe
from frappe.utils import escape_html


REOPEN_AUDIT_PREFIX = "QAS invoice reopened for correction."


def reopen_same_invoice(doc, reason):
    snapshot = doc.as_json()
    # Run standard reversal, allocation/link checks and cancellation hooks first.
    # The caller owns the transaction and rolls everything back on failure.
    doc.cancel()
    for field in doc.meta.get_table_fields():
        frappe.db.set_value(field.options, {"parent": doc.name, "parenttype": "Sales Invoice"},
                            "docstatus", 0, update_modified=False)
    frappe.db.set_value("Sales Invoice", doc.name,
                        {"docstatus": 0, "status": "Draft", "outstanding_amount": 0})
    frappe.clear_document_cache("Sales Invoice", doc.name)
    doc = frappe.get_doc("Sales Invoice", doc.name)
    doc.add_comment("Info", REOPEN_AUDIT_PREFIX + " Original number retained. Reason: " + escape_html(reason)
                    + "<details><summary>Invoice before reopening</summary><pre>"
                    + escape_html(snapshot) + "</pre></details>")
    return doc


def reopened_invoice_revision(invoice):
    return frappe.db.get_value("Comment", {
        "reference_doctype": "Sales Invoice", "reference_name": invoice,
        "comment_type": "Info", "content": ["like", REOPEN_AUDIT_PREFIX + "%"],
    }, "name", order_by="creation desc")
