"""Thin School Admin entry points for PAYG commands and audit records."""
import frappe

from qas_custom.modules.billing import payg_drafts
from qas_custom.modules.payg import card_admin, cancellation, issue
from qas_custom.services import payg_read_models
from qas_custom.services.support_view import get_support_view_parent, get_support_view_token


def _admin():
    if get_support_view_token() or "School Admin" not in frappe.get_roles(frappe.session.user):
        raise frappe.PermissionError("School Admin access is required")


def _admin_read(family_parent=None):
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        raise frappe.PermissionError("School Admin access is required")
    if get_support_view_token():
        support_parent = get_support_view_parent()
        if family_parent and family_parent != support_parent.name:
            raise frappe.PermissionError("Support View can only read its selected family")


def _required_key(value):
    if not str(value or "").strip():
        frappe.throw("PAYG request key is required")
    return value


def _operation_payload(operation):
    return {"operation": operation.name, "operation_type": operation.operation_type,
            "family_parent": operation.family_parent, "product": operation.product,
            "status": operation.status, "card": operation.card, "invoice": operation.invoice,
            "source_card": operation.source_card, "target_card": operation.target_card,
            "transferred_quantity": operation.quantity, "price_delta": operation.price_delta}


@frappe.whitelist()
def payg_admin_context(family_parent=None):
    _admin_read(family_parent)
    products = payg_read_models.product_payloads()
    if not family_parent:
        return {"products": products}
    if not frappe.db.exists("Parent", family_parent):
        frappe.throw("PAYG family Parent was not found")
    students = frappe.get_all("Student", filters={"guardian": family_parent},
                              fields=["name", "student_name", "status", "age"],
                              order_by="student_name asc", limit_page_length=0)
    cards = frappe.get_all("QAS PAYG Card", filters={"family_parent": family_parent},
                           fields=["name", "family_parent", "customer", "product", "course",
                                   "issued_on", "expires_on", "unit_price_snapshot", "status",
                                   "available_count", "reserved_count", "consumed_count"],
                           order_by="creation desc", limit_page_length=0)
    bookings = frappe.get_all("QAS PAYG Booking", filters={"family_parent": family_parent},
                              fields=["name", "student", "card", "course_session", "attendance_entry",
                                      "status", "cancellable_until", "cancelled_at", "card_expires_on_snapshot"],
                              order_by="creation desc", limit_page_length=0)
    return {"family_parent": family_parent, "products": products, "students": students,
            "cards": payg_read_models.enrich_cards(cards),
            "bookings": payg_read_models.enrich_booking_history(bookings, cards=cards)}


@frappe.whitelist()
def payg_create_purchase_operation(family_parent=None, product=None, request_key=None):
    _admin()
    operation = issue.create_or_get_purchase_operation(family_parent, product, _required_key(request_key))
    return _operation_payload(operation)


@frappe.whitelist()
def payg_get_operation(operation_id=None):
    _admin()
    if not operation_id:
        frappe.throw("PAYG operation is required")
    return _operation_payload(frappe.get_doc("QAS PAYG Operation", operation_id))


@frappe.whitelist()
def payg_issue_card(operation_id=None, request_key=None):
    _admin()
    card = issue.issue_card(operation_id, _required_key(request_key))
    return {"card": card.name, "operation": operation_id, "status": card.status,
            "available_count": card.available_count, "reserved_count": card.reserved_count,
            "consumed_count": card.consumed_count}


@frappe.whitelist()
def payg_create_invoice_draft(operation_id=None, request_key=None):
    _admin()
    result = payg_drafts.create_payg_draft(operation_id, _required_key(request_key))
    if isinstance(result, dict):
        return result
    return {"operation": operation_id, "invoice": result.name}


@frappe.whitelist()
def payg_change_expiry(card_id=None, new_expiry=None, reason=None, request_key=None):
    _admin()
    result = card_admin.change_expiry(card_id, new_expiry, reason=reason, request_key=_required_key(request_key))
    return _operation_payload(result)


@frappe.whitelist()
def payg_exchange_card(card_id=None, target_product_id=None, request_key=None, invoice_request_key=None):
    _admin()
    result = payg_drafts.exchange_card_with_draft(
        card_id, target_product_id, _required_key(request_key), _required_key(invoice_request_key))
    operation = result["operation"]
    payload = _operation_payload(operation)
    payload["invoice"] = result["invoice"].name if result["invoice"] else None
    payload["draft_reason"] = result["reason"]
    payload["manual_refund_review_required"] = bool(operation.price_delta and operation.price_delta < 0)
    return payload


@frappe.whitelist()
def payg_admin_cancel_booking(booking_id=None, reason=None, request_key=None):
    _admin()
    result = cancellation.cancel_by_admin(booking_id, reason=reason, request_key=_required_key(request_key))
    return {"booking": result.name, "status": result.status, "card": result.card}
