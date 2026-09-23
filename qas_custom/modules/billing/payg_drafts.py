"""One independently reviewed invoice draft per PAYG purchase or paid exchange."""
from decimal import Decimal
from uuid import uuid4

import frappe

from qas_custom.modules.billing.commands import get_invoice_item, run_invoice_mutation_as_administrator
from qas_custom.modules.billing.drafts import new_invoice_draft
from qas_custom.modules.billing.invoice_settings import apply_invoice_payment_snapshot
from qas_custom.modules.notifications.guard import disable_sales_invoice_auto_notifications
from qas_custom.modules.payg.invoice_links import allow_invoice_relink
from qas_custom.services.support_view import get_support_view_token


def _admin():
    if get_support_view_token():
        frappe.throw("Support View cannot create PAYG drafts", frappe.PermissionError)
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw("School Admin access is required", frappe.PermissionError)


def reject_payg_support_view_write(locked_operations):
    """Keep legacy invoices unchanged while PAYG bindings remain read-only."""
    if locked_operations and get_support_view_token():
        frappe.throw("Support View cannot change PAYG invoices", frappe.PermissionError)


def _linked_invoice(operation):
    invoice = frappe.get_doc("Sales Invoice", operation.invoice, for_update=True)
    if invoice.customer != operation.customer or invoice.get("parent") != operation.family_parent:
        frappe.throw("PAYG invoice customer or family does not match operation")
    matching = [row for row in (invoice.get("items") or [])
                if (row.get("qas_source_doctype"), row.get("qas_source_document")) ==
                ("QAS PAYG Operation", operation.name)]
    if len(matching) != 1:
        frappe.throw("PAYG invoice line source does not match operation")
    return invoice


def payg_sources(invoice):
    """Return row-name/operation pairs; reject incomplete provenance."""
    result = {}
    seen = set()
    for row in invoice.get("items") or []:
        doctype, operation = row.get("qas_source_doctype"), row.get("qas_source_document")
        if not doctype and not operation:
            continue
        if doctype and doctype != "QAS PAYG Operation":
            continue
        if doctype != "QAS PAYG Operation" or not operation or operation in seen or not row.get("name"):
            frappe.throw("Invalid or duplicate PAYG invoice line source")
        seen.add(operation)
        result[row.name] = operation
    if invoice.get("qas_invoice_type") == "PAYG Card" and not result:
        frappe.throw("PAYG invoice line source is missing")
    return result


def validate_payg_bindings(invoice, locked_operations):
    sources = payg_sources(invoice)
    if set(sources.values()) != set(locked_operations):
        frappe.throw("PAYG invoice sources changed; retry")
    for operation_id in sources.values():
        operation = locked_operations[operation_id]
        if (operation.invoice != invoice.name or operation.customer != invoice.customer
                or operation.family_parent != invoice.get("parent")):
            frappe.throw("PAYG invoice source family/customer or link does not match operation")
    return sources


def lock_payg_operations_for_invoices(invoice_names):
    """Lock Operations before any Invoice row lock, in a stable global order."""
    ids = set(frappe.get_all("QAS PAYG Operation", filters={"invoice": ["in", invoice_names]},
                             pluck="name", limit_page_length=0))
    for invoice_name in invoice_names:
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        ids.update(payg_sources(invoice).values())
    return {name: frappe.get_doc("QAS PAYG Operation", name, for_update=True)
            for name in sorted(ids)}


def relink_consolidated_payg_operations(invoices, target, locked_operations):
    selected = {invoice.name for invoice in invoices}
    target_sources = payg_sources(target)
    if set(target_sources.values()) != set(locked_operations):
        frappe.throw("Consolidated PAYG invoice sources changed; retry")
    for operation_id in sorted(locked_operations):
        operation = locked_operations[operation_id]
        if operation.invoice not in selected or operation.customer != target.customer or operation.family_parent != target.get("parent"):
            frappe.throw("PAYG operation cannot be consolidated into another family")
        if operation.invoice == target.name:
            continue
        source = operation.invoice
        operation.invoice = target.name
        with allow_invoice_relink(operation.name, source, target.name):
            operation.save(ignore_permissions=True)


def create_payg_draft(operation_id, invoice_request_key):
    """Bill an existing operation; never create or submit an operation or invoice."""
    _admin()
    if not operation_id or not str(invoice_request_key or "").strip():
        frappe.throw("Operation and invoice request key are required")
    savepoint = "payg_invoice_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        return _create_payg_draft(operation_id, invoice_request_key)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _create_payg_draft(operation_id, invoice_request_key):
    # Shared serializer with issue_card. The row must pre-exist: a missing ID is
    # an error and must never be interpreted as a request to purchase.
    operation = frappe.get_doc("QAS PAYG Operation", operation_id, for_update=True)
    if operation.operation_type not in ("Purchase", "Exchange") or operation.status == "Cancelled":
        frappe.throw("Only an active Purchase or Exchange operation can be invoiced")
    if operation.invoice:
        if operation.invoice_request_key != invoice_request_key:
            frappe.throw("PAYG operation already has an invoice with another key")
        return _linked_invoice(operation)
    if operation.invoice_request_key and operation.invoice_request_key != invoice_request_key:
        frappe.throw("PAYG operation has another invoice request key")
    if operation.operation_type == "Exchange":
        if not operation.target_card or operation.status != "Completed":
            frappe.throw("Exchange must complete before invoicing")
        rate = Decimal(str(operation.price_delta or 0))
        if rate <= 0:
            return {"invoice": None, "reason": "non_positive_exchange_delta",
                    "operation": operation.name, "price_delta": rate}
        course = operation.new_course
        line_type = "PAYG Exchange"
        description = f"PAYG course exchange to {course} ({operation.quantity} sessions)"
    else:
        course = None
        line_type = "PAYG Card"
        description = None
    frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE",
                  (operation.family_parent,))
    customer = frappe.db.get_value("Parent", operation.family_parent, "customer")
    if not customer or customer != operation.customer:
        frappe.throw("PAYG operation family/customer no longer match")
    product = frappe.get_doc("QAS PAYG Product", operation.product, for_update=True)
    if not product.course or (course and course != product.course):
        frappe.throw("PAYG product/course no longer match operation")
    course = course or product.course
    if operation.operation_type == "Purchase":
        if not product.enabled:
            frappe.throw("PAYG purchase product is disabled")
        rate = Decimal(str(product.standard_card_price or 0))
        if rate <= 0:
            frappe.throw("PAYG standard card price must be positive")
        description = f"PAYG 10-session card for {course}"
    if product.invoice_item:
        if not frappe.db.exists("Item", product.invoice_item):
            frappe.throw("PAYG Invoice Item does not exist")
        item_code = product.invoice_item
    else:
        item_code = get_invoice_item(course)
    disable_sales_invoice_auto_notifications()
    invoice = new_invoice_draft(customer=customer, parent=operation.family_parent,
                                invoice_type="PAYG Card")
    invoice.append("items", {"item_code": item_code, "item_name": course, "qty": 1, "rate": rate,
                                    "description": description, "course": course,
                                    "qas_line_type": line_type,
                                    "qas_source_doctype": "QAS PAYG Operation",
                                    "qas_source_document": operation.name})
    apply_invoice_payment_snapshot(invoice)
    run_invoice_mutation_as_administrator(lambda: invoice.insert(ignore_permissions=True))
    operation.invoice = invoice.name
    operation.invoice_request_key = invoice_request_key
    operation.save(ignore_permissions=True)
    return invoice


def exchange_card_with_draft(card_id, target_product_id, request_key, invoice_request_key, *, at=None):
    """Transfer and bill a positive exchange in one caller-owned transaction."""
    _admin()
    from qas_custom.modules.payg.card_admin import exchange_card

    savepoint = "payg_exchange_invoice_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        operation = exchange_card(card_id, target_product_id, request_key, at=at)
        result = create_payg_draft(operation.name, invoice_request_key)
        return {"operation": operation, "invoice": result if not isinstance(result, dict) else None,
                "reason": result.get("reason") if isinstance(result, dict) else None}
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
