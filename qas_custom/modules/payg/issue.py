"""Issue a ten-session PAYG card against one stable Purchase operation."""
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import get_datetime_in_timezone

from qas_custom.modules.payg.money import stored_currency
from qas_custom.modules.payg.rules import add_six_months


def _admin():
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw("School Admin access is required", frappe.PermissionError)


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _purchase_by_request_key(request_key, *, after_duplicate=False):
    if after_duplicate:
        rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Operation`
            WHERE operation_type='Purchase' AND request_key=%s FOR UPDATE""",
            (request_key,), as_dict=True)
        name = rows[0].name if rows else None
    else:
        # Missing-key locking reads take RR gap locks and can deadlock inserts.
        name = frappe.db.get_value("QAS PAYG Operation",
                                   {"operation_type": "Purchase", "request_key": request_key}, "name")
    return frappe.get_doc("QAS PAYG Operation", name, for_update=True) if name else None


def create_or_get_purchase_operation(family_parent, product, purchase_request_key):
    _admin()
    if not all((family_parent, product, purchase_request_key)):
        frappe.throw("Family, product and purchase request key are required")
    operation = _purchase_by_request_key(purchase_request_key)
    if operation:
        if (operation.family_parent, operation.product, operation.customer) != (
                family_parent, product, frappe.db.get_value("Parent", family_parent, "customer")):
            frappe.throw("Purchase request key belongs to another purchase")
        return operation
    customer = frappe.db.get_value("Parent", family_parent, "customer")
    product_doc = frappe.get_doc("QAS PAYG Product", product, for_update=True)
    if not customer or not product_doc.enabled or not product_doc.course:
        frappe.throw("Family customer and enabled PAYG product are required")
    unit_price = stored_currency(Decimal(str(product_doc.standard_card_price or 0)) / 10)
    if unit_price <= 0:
        frappe.throw("PAYG standard card price must be positive")
    savepoint = "payg_purchase_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        operation = frappe.get_doc({"doctype": "QAS PAYG Operation", "operation_type": "Purchase",
                                    "request_key": purchase_request_key, "family_parent": family_parent,
                                    "customer": customer, "product": product, "quantity": 10,
                                    "new_price": unit_price, "new_course": product_doc.course,
                                    "actor": frappe.session.user, "created_at": _now(), "status": "Pending"})
        operation.insert(ignore_permissions=True)
        return operation
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        operation = _purchase_by_request_key(purchase_request_key, after_duplicate=True)
        if operation:
            if (operation.family_parent, operation.product, operation.customer) != (
                    family_parent, product, customer):
                frappe.throw("Purchase request key belongs to another purchase")
            return operation
        raise
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def issue_card(operation_id, issue_request_key):
    _admin()
    if not operation_id or not issue_request_key:
        frappe.throw("Operation and issue request key are required")
    savepoint = "payg_issue_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        return _issue_card(operation_id, issue_request_key)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _issue_card(operation_id, issue_request_key):
    # The operation row is the serializer for both issue and invoice actions.
    operation = frappe.get_doc("QAS PAYG Operation", operation_id, for_update=True)
    if operation.operation_type != "Purchase" or operation.status == "Cancelled":
        frappe.throw("Only an active Purchase operation can issue a card")
    if operation.card:
        if operation.issue_request_key != issue_request_key:
            frappe.throw("Purchase operation has already issued a card with another key")
        return frappe.get_doc("QAS PAYG Card", operation.card)
    if operation.issue_request_key and operation.issue_request_key != issue_request_key:
        frappe.throw("Purchase operation has another issue request key")
    frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE", (operation.family_parent,))
    customer = frappe.db.get_value("Parent", operation.family_parent, "customer")
    product = frappe.get_doc("QAS PAYG Product", operation.product, for_update=True)
    if (not customer or customer != operation.customer or not product.enabled
            or not product.course or product.course != operation.new_course):
        frappe.throw("Purchase family/customer/product is no longer valid")
    unit_price = stored_currency(operation.new_price or 0)
    if unit_price <= 0:
        frappe.throw("Purchase operation price snapshot is missing")
    now = _now()
    issued_on = now.date()
    card = frappe.get_doc({"doctype": "QAS PAYG Card", "family_parent": operation.family_parent,
                           "customer": customer, "product": operation.product, "course": product.course,
                           "issued_on": issued_on, "expires_on": add_six_months(issued_on),
                           "unit_price_snapshot": unit_price,
                           "available_count": 0, "reserved_count": 0, "consumed_count": 0,
                           "status": "Active"})
    card.insert(ignore_permissions=True)
    operation.card = card.name
    operation.issue_request_key = issue_request_key
    operation.save(ignore_permissions=True)
    frappe.get_doc({"doctype": "QAS PAYG Entry", "card": card.name, "operation": operation.name,
                    "kind": "Issue", "available_delta": 10, "reserved_delta": 0,
                    "consumed_delta": 0, "operation_key": f"issue:{operation.name}",
                    "actor": frappe.session.user, "occurred_at": now}).insert(ignore_permissions=True)
    return frappe.get_doc("QAS PAYG Card", card.name)
