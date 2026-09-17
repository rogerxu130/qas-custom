"""Keep trial invoice deadlines aligned with the booked lesson, without sending email."""
import frappe
from frappe.utils import cint, flt, getdate

from qas_custom.modules.billing.invoice_settings import course_invoice_due_date


def sync_trial_invoice_dates(invoice, inquiry, *, dry_run=False):
    if (cint(invoice.get("docstatus")) == 2 or invoice.get("is_return")
        or invoice.get("qas_has_payment_plan")
        or (cint(invoice.get("docstatus")) == 1 and flt(invoice.get("outstanding_amount")) <= 0)
        or inquiry.get("inquiry_type") != "Trial Lesson"
        or inquiry.get("status") == "Cancelled"
        or not inquiry.get("course_session") or not invoice.get("posting_date")):
        return None
    first_class = frappe.db.get_value("Course Sessions", inquiry.course_session, "session_date")
    if not first_class:
        return None
    due_date = course_invoice_due_date(invoice.posting_date, first_class)
    schedule = invoice.get("payment_schedule") or []
    # Preserve separately negotiated multi-installment schedules too.
    if len(schedule) > 1:
        return None
    if invoice.get("due_date") and getdate(invoice.due_date) == getdate(due_date) and all(
        row.get("due_date") and getdate(row.due_date) == getdate(due_date) for row in schedule
    ):
        return None
    change = {"invoice": invoice.name, "inquiry": inquiry.name,
              "old_due_date": str(invoice.get("due_date") or ""), "due_date": str(due_date)}
    if dry_run:
        return change
    invoice.db_set("due_date", due_date, notify=False)
    for row in schedule:
        row.db_set("due_date", due_date, notify=False)
    if cint(invoice.docstatus) == 1:
        # Keep accounting ageing reports consistent with the invoice and payment schedule.
        for doctype in ("GL Entry", "Payment Ledger Entry"):
            frappe.db.set_value(doctype, {"voucher_type": "Sales Invoice", "voucher_no": invoice.name},
                                "due_date", due_date, update_modified=False)
        invoice.set_status(update=True)
    invoice.add_comment("Info", "Trial invoice due date corrected from {old_due_date} to {due_date} "
                        "using the booked lesson date (Inquiry {inquiry}).".format(**change))
    return change


def repair_trial_invoice_dates(*, dry_run=True):
    """Preview by default; migration applies updates in its existing transaction."""
    changes = []
    for row in frappe.get_all("Sales Invoice", filters={
        "docstatus": ["<", 2], "is_return": 0, "source_doctype": "Inquiry",
        "source_type": ["in", ["Trial Inquiry", "Replacement Trial Inquiry"]],
    }, fields=["name", "source_document"], limit_page_length=0):
        if not row.source_document or not frappe.db.exists("Inquiry", row.source_document):
            continue
        change = sync_trial_invoice_dates(frappe.get_doc("Sales Invoice", row.name),
                                         frappe.get_doc("Inquiry", row.source_document), dry_run=dry_run)
        if change:
            changes.append(change)
    return changes
