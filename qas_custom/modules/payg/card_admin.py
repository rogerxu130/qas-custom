"""Internal School Admin commands for PAYG expiry and whole-card exchange.

Each command joins the caller's transaction. The positive exchange invoice is a
separate Task 7 concern; this module never creates an invoice or credits a family.
"""
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe.utils import getdate, get_time, get_datetime_in_timezone

from qas_custom.modules.payg.rules import add_six_months, as_brisbane_datetime
from qas_custom.services.support_view import get_support_view_token


CARD = "QAS PAYG Card"
BOOKING = "QAS PAYG Booking"
OPERATION = "QAS PAYG Operation"
ENTRY = "QAS PAYG Entry"


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _admin():
    if get_support_view_token():
        frappe.throw("Support View cannot change PAYG cards", frappe.PermissionError)
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw("School Admin access is required", frappe.PermissionError)


def _operation_by_key(kind, request_key):
    rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Operation`
        WHERE operation_type=%s AND request_key=%s FOR UPDATE""",
        (kind, request_key), as_dict=True)
    return frappe.get_doc(OPERATION, rows[0].name, for_update=True) if rows else None


def _bookings(card_id):
    # Current read, including status changes committed while Student/Parent locks
    # were awaited. Cancelled rows remain historical and cannot block a change.
    rows = frappe.db.sql("""SELECT name, student FROM `tabQAS PAYG Booking`
        WHERE card=%s AND status!='Cancelled' ORDER BY student, name FOR UPDATE""",
        (card_id,), as_dict=True)
    return [frappe.get_doc(BOOKING, row.name, for_update=True) for row in rows]


def _lock_context(card_id, kind, request_key):
    family = frappe.db.get_value(CARD, card_id, "family_parent")
    if not family:
        frappe.throw("PAYG source card does not exist")
    # Booking creation locks Student, then Parent, then Card. Lock all family
    # students first so a booking cannot appear after the Parent lock is acquired.
    frappe.db.sql("""SELECT name FROM `tabStudent` WHERE guardian=%s
        ORDER BY name FOR UPDATE""", (family,), as_dict=True)
    frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE", (family,))
    bookings = _bookings(card_id)
    operation = _operation_by_key(kind, request_key)
    card = frappe.get_doc(CARD, card_id, for_update=True)
    if card.family_parent != family or card.customer != frappe.db.get_value("Parent", family, "customer"):
        frappe.throw("PAYG card family/customer changed; retry")
    # Recheck after the card lock. This is intentionally a locking current read,
    # not frappe.get_all against a transaction's older snapshot.
    latest = _bookings(card_id)
    if [(b.name, b.status) for b in latest] != [(b.name, b.status) for b in bookings]:
        frappe.throw("PAYG bookings changed during card administration; retry")
    return card, latest, operation


def _booking_start(booking):
    session = frappe.get_doc("Course Sessions", booking.course_session, for_update=True)
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot, for_update=True)
    if not session.session_date or not slot.start_time:
        frappe.throw(f"PAYG booking {booking.name} has no session start")
    return as_brisbane_datetime(datetime.combine(getdate(session.session_date), get_time(slot.start_time)))


def _same_expiry(operation, card_id, expiry, reason):
    if (operation.card, getdate(operation.new_expiry), operation.reason) != (card_id, expiry, reason):
        frappe.throw("Expiry request key belongs to another change")
    return operation


def change_expiry(card_id, new_expiry, *, reason, request_key):
    """Change only card expiry, retaining an immutable old/new audit operation."""
    _admin()
    if not card_id or not new_expiry or not str(reason or "").strip() or not str(request_key or "").strip():
        frappe.throw("Card, new expiry, reason and request key are required")
    expiry = getdate(new_expiry)
    reason = reason.strip()
    savepoint = "payg_expiry_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        return _change_expiry(card_id, expiry, reason, request_key)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        # The unique key may have been won by an identical concurrent request.
        return _change_expiry(card_id, expiry, reason, request_key)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _change_expiry(card_id, expiry, reason, request_key):
    card, bookings, existing = _lock_context(card_id, "ExpiryChange", request_key)
    if existing:
        return _same_expiry(existing, card_id, expiry, reason)
    if expiry < getdate(card.issued_on):
        frappe.throw("PAYG card expiry cannot precede issue date")
    starts = [(booking.name, _booking_start(booking)) for booking in bookings]
    blockers = [f"{name} ({start.isoformat()})" for name, start in starts
                if start.date() > expiry]
    if blockers:
        frappe.throw("PAYG expiry would exclude bookings: " + ", ".join(blockers))
    old = getdate(card.expires_on)
    operation = frappe.get_doc({"doctype": OPERATION, "operation_type": "ExpiryChange",
                                "request_key": request_key, "card": card.name,
                                "family_parent": card.family_parent, "customer": card.customer,
                                "old_expiry": old, "new_expiry": expiry, "reason": reason,
                                "quantity": 0, "actor": frappe.session.user,
                                "created_at": _now(), "status": "Pending"})
    operation.insert(ignore_permissions=True)
    card.expires_on = expiry
    card.save(ignore_permissions=True)
    operation.status = "Completed"
    operation.save(ignore_permissions=True)
    return operation


def _same_exchange(operation, card_id, target_product_id, requested_issue_date=None):
    if (operation.source_card, operation.product) != (card_id, target_product_id):
        frappe.throw("Exchange request key belongs to another change")
    if requested_issue_date is not None:
        issued = frappe.db.get_value(CARD, operation.target_card, "issued_on")
        if getdate(issued) != requested_issue_date:
            frappe.throw("Exchange request key belongs to another issue date")
    return operation


def exchange_card(card_id, target_product_id, request_key, *, at=None):
    """Transfer every currently available unit into a new card for another course."""
    _admin()
    if not all((card_id, target_product_id, str(request_key or "").strip())):
        frappe.throw("Source card, target product and request key are required")
    now = _now()
    issued_on = getdate(at) if at is not None else now.date()
    savepoint = "payg_exchange_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        return _exchange_card(card_id, target_product_id, request_key, issued_on, now,
                              explicit_date=at is not None)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        return _exchange_card(card_id, target_product_id, request_key, issued_on, now,
                              explicit_date=at is not None)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _exchange_card(card_id, target_product_id, request_key, issued_on, now, *, explicit_date):
    source, bookings, existing = _lock_context(card_id, "Exchange", request_key)
    if existing:
        return _same_exchange(existing, card_id, target_product_id,
                              issued_on if explicit_date else None)
    if source.status not in ("Active", "Transferred"):
        frappe.throw("PAYG source card is unavailable for exchange")
    future = [f"{booking.name} ({start.isoformat()})" for booking in bookings
              if (start := _booking_start(booking)) > as_brisbane_datetime(now)]
    if future:
        frappe.throw("Cancel active future PAYG bookings manually before exchange: " + ", ".join(future))
    quantity = int(source.available_count)
    if not 1 <= quantity <= 10:
        frappe.throw("Exchange requires 1 to 10 available sessions")
    product = frappe.get_doc("QAS PAYG Product", target_product_id, for_update=True)
    if not product.enabled or not product.course or product.course == source.course:
        frappe.throw("Exchange requires an enabled product for another course")
    course = frappe.get_doc("Course", product.course)
    if course.get("status") not in (None, "Active"):
        frappe.throw("Exchange target course is inactive")
    old_price = Decimal(str(source.unit_price_snapshot))
    new_price = Decimal(str(product.standard_card_price)) / Decimal(10)
    delta = (new_price - old_price) * quantity
    operation = frappe.get_doc({"doctype": OPERATION, "operation_type": "Exchange",
                                "request_key": request_key, "source_card": source.name,
                                "family_parent": source.family_parent, "customer": source.customer,
                                "product": product.name, "old_course": source.course,
                                "new_course": product.course, "old_price": old_price,
                                "new_price": new_price, "quantity": quantity,
                                "price_delta": delta, "actor": frappe.session.user,
                                "created_at": now, "status": "Pending"})
    operation.insert(ignore_permissions=True)
    target = frappe.get_doc({"doctype": CARD, "family_parent": source.family_parent,
                             "customer": source.customer, "product": product.name,
                             "course": product.course, "issued_on": issued_on,
                             "expires_on": add_six_months(issued_on),
                             "unit_price_snapshot": new_price,
                             "available_count": 0, "reserved_count": 0,
                             "consumed_count": 0, "status": "Active"})
    target.insert(ignore_permissions=True)
    operation.target_card = target.name
    operation.status = "Completed"
    operation.save(ignore_permissions=True)
    for card, kind, amount, direction in (
            (source, "Transfer Out", -quantity, "transfer-out"),
            (target, "Transfer In", quantity, "transfer-in")):
        frappe.get_doc({"doctype": ENTRY, "card": card.name, "operation": operation.name,
                        "kind": kind, "available_delta": amount,
                        "reserved_delta": 0, "consumed_delta": 0,
                        "operation_key": f"{direction}:{operation.name}",
                        "actor": frappe.session.user, "occurred_at": now}).insert(ignore_permissions=True)
    # Entry.after_insert updates the cached balance in SQL. Reload before saving
    # status, otherwise Card.validate sees the old in-memory available_count.
    source = frappe.get_doc(CARD, source.name, for_update=True)
    source.status = "Transferred"
    source.save(ignore_permissions=True)
    return operation
