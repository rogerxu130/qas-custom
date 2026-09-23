"""Issue a ten-session PAYG card against one stable Purchase operation."""
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import get_datetime_in_timezone

from qas_custom.modules.payg.rules import add_six_months


def _admin():
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw("School Admin access is required", frappe.PermissionError)


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _purchase_by_request_key(request_key):
    rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Operation`
        WHERE operation_type='Purchase' AND request_key=%s FOR UPDATE""",
        (request_key,), as_dict=True)
    return frappe.get_doc("QAS PAYG Operation", rows[0].name, for_update=True) if rows else None


def create_or_get_purchase_operation(family_parent, product, purchase_request_key):
    _admin()
    if not all((family_parent, product, purchase_request_key)):
        frappe.throw("Family, product and purchase request key are required")
    operation = _purchase_by_request_key(purchase_request_key)
    if operation:
        if (operation.family_parent, operation.product) != (family_parent, product):
            frappe.throw("Purchase request key belongs to another purchase")
        return operation
    customer = frappe.db.get_value("Parent", family_parent, "customer")
    enabled = frappe.db.get_value("QAS PAYG Product", product, "enabled")
    if not customer or not enabled:
        frappe.throw("Family customer and enabled PAYG product are required")
    savepoint = "payg_purchase_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        operation = frappe.get_doc({"doctype": "QAS PAYG Operation", "operation_type": "Purchase",
                                    "request_key": purchase_request_key, "family_parent": family_parent,
                                    "customer": customer, "product": product, "quantity": 10,
                                    "actor": frappe.session.user, "created_at": _now(), "status": "Pending"})
        operation.insert(ignore_permissions=True)
        return operation
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        operation = _purchase_by_request_key(purchase_request_key)
        if operation:
            if (operation.family_parent, operation.product) != (family_parent, product):
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
    if not customer or customer != operation.customer or not product.enabled or not product.course:
        frappe.throw("Purchase family/customer/product is no longer valid")
    now = _now()
    issued_on = now.date()
    card = frappe.get_doc({"doctype": "QAS PAYG Card", "family_parent": operation.family_parent,
                           "customer": customer, "product": operation.product, "course": product.course,
                           "issued_on": issued_on, "expires_on": add_six_months(issued_on),
                           "unit_price_snapshot": Decimal(str(product.standard_card_price)) / 10,
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
