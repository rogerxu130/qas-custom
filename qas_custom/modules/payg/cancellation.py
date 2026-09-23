"""Audit PAYG returns and consumption in the caller's transaction."""
import json
from uuid import uuid4

import frappe
from frappe.utils import get_datetime_in_timezone

from qas_custom.modules.course_schedule import session_resources
from qas_custom.modules.payg.rules import as_brisbane_datetime
from qas_custom.services.support_view import get_support_view_token


BOOKING = "QAS PAYG Booking"
ENTRY = "QAS PAYG Entry"


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _locked_booking(booking_id):
    # This first lookup is read-only: Student must be the first row lock.
    student = frappe.db.get_value(BOOKING, booking_id, "student")
    if not student:
        frappe.throw("PAYG booking does not exist")
    frappe.db.sql("SELECT name FROM `tabStudent` WHERE name=%s FOR UPDATE", (student,))
    booking = frappe.get_doc(BOOKING, booking_id, for_update=True)
    if booking.student != student:
        frappe.throw("PAYG booking student changed; retry")
    card = frappe.get_doc("QAS PAYG Card", booking.card, for_update=True)
    session = frappe.get_doc("Course Sessions", booking.course_session, for_update=True)
    slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot, for_update=True)
    frappe.get_doc("Term", slot.term, for_update=True)
    if slot.classroom:
        frappe.get_doc("Classroom", slot.classroom, for_update=True)
    session_resources.active_rows(session.name, lock=True)
    if not booking.attendance_entry:
        frappe.throw("PAYG booking attendance was not found")
    attendance = frappe.get_doc("Class Attendance Entry", booking.attendance_entry, for_update=True)
    if (attendance.student, attendance.course_session,
            attendance.source_doctype, attendance.source_document) != (
            booking.student, booking.course_session, BOOKING, booking.name):
        frappe.throw("PAYG booking attendance does not match its source")
    if attendance.status == "Cancelled" and booking.status != "Cancelled":
        frappe.throw("PAYG booking attendance is Cancelled while booking is open")
    return booking, card, attendance


def _return_key(booking_id):
    return f"return:{booking_id}"


def _prior_return_key(booking_id):
    rows = frappe.db.sql("""SELECT reason FROM `tabQAS PAYG Entry`
        WHERE operation_key=%s FOR UPDATE""", (_return_key(booking_id),), as_dict=True)
    if not rows or not rows[0].reason:
        return None
    try:
        audit = json.loads(rows[0].reason)
    except (TypeError, ValueError):
        return None
    return audit.get("admin_request_key") if isinstance(audit, dict) else None


def _has_consume(booking_id):
    return bool(frappe.db.sql("""SELECT name FROM `tabQAS PAYG Entry`
        WHERE booking=%s AND kind='Consume' FOR UPDATE""", (booking_id,), as_dict=True))


def cancel_by_admin(booking_id, *, reason, request_key, admin=None):
    """Return a booking's single unit to its original card, including after class."""
    if get_support_view_token():
        frappe.throw("Support View cannot write PAYG bookings", frappe.PermissionError)
    if "School Admin" not in frappe.get_roles(frappe.session.user):
        frappe.throw("School Admin access is required", frappe.PermissionError)
    if not booking_id or not str(reason or "").strip() or not str(request_key or "").strip():
        frappe.throw("Booking, cancellation reason and request key are required")
    savepoint = "payg_admin_cancel_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        booking, card, attendance = _locked_booking(booking_id)
        if booking.status == "Cancelled":
            if _prior_return_key(booking.name) != request_key:
                frappe.throw("Booking was already cancelled with another request key")
            return booking
        if booking.status not in ("Reserved", "Locked", "Completed"):
            frappe.throw("PAYG booking cannot be cancelled from this status")
        consumed = booking.status in ("Locked", "Completed")
        if consumed != _has_consume(booking.name):
            frappe.throw("PAYG booking status and consumption differ")
        now = _now()
        audit = json.dumps({"admin_request_key": request_key, "reason": reason}, ensure_ascii=False,
                           separators=(",", ":"))
        frappe.get_doc({"doctype": ENTRY, "card": card.name, "booking": booking.name,
                        "kind": "Return", "available_delta": 1,
                        "reserved_delta": 0 if consumed else -1,
                        "consumed_delta": -1 if consumed else 0,
                        "operation_key": _return_key(booking.name), "reason": audit,
                        "actor": frappe.session.user, "occurred_at": now}).insert(ignore_permissions=True)
        booking.status = "Cancelled"
        booking.cancelled_at = now
        booking.cancelled_by = frappe.session.user
        booking.cancel_reason = reason.strip()
        booking.save(ignore_permissions=True)
        # Preserve source, marked_by, marked_at and the historical row itself.
        frappe.db.set_value("Class Attendance Entry", booking.attendance_entry, "status", "Cancelled")
        return booking
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def lock_due_booking(booking_id):
    """Consume one Reserved booking if its real-time cancellation deadline passed."""
    if not booking_id:
        frappe.throw("Booking is required")
    savepoint = "payg_lock_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        booking, card, attendance = _locked_booking(booking_id)
        if booking.status != "Reserved":
            return False
        now = _now()
        if not booking.cancellable_until or as_brisbane_datetime(booking.cancellable_until) > as_brisbane_datetime(now):
            return False
        frappe.get_doc({"doctype": ENTRY, "card": card.name, "booking": booking.name,
                        "kind": "Consume", "available_delta": 0, "reserved_delta": -1,
                        "consumed_delta": 1, "operation_key": f"consume:{booking.name}",
                        "actor": frappe.session.user, "occurred_at": now}).insert(ignore_permissions=True)
        booking.status = "Locked"
        booking.save(ignore_permissions=True)
        return True
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
