"""Parent-facing PAYG adapters. Family identity is always resolved by the service."""
import json
import frappe
from frappe.utils import getdate, today

from qas_custom.modules.payg import booking


def _card_payload(card):
    expired = bool(card.expires_on and getdate(card.expires_on) < getdate(today()))
    return {
        "name": card.name, "course": card.course, "product": card.product,
        "issued_on": card.issued_on, "expires_on": card.expires_on,
        "status": "Expired" if expired and card.status == "Active" else card.status,
        "available_count": card.available_count, "reserved_count": card.reserved_count,
        "consumed_count": card.consumed_count,
        "bookable": card.status == "Active" and not expired and int(card.available_count or 0) > 0,
    }


def _booking_payload(row):
    return {"name": row.name, "student": row.student, "card": row.card,
            "course_session": row.course_session, "attendance_entry": row.attendance_entry,
            "status": row.status, "cancellable_until": row.cancellable_until,
            "cancelled_at": row.cancelled_at}


@frappe.whitelist()
def payg_family_cards(student=None):
    result = booking.family_cards(student=student)
    return {"students": result["students"],
            "cards": [_card_payload(card) for card in result["cards"]]}


@frappe.whitelist()
def payg_available_sessions(student=None, course=None, cursor=None, limit=30):
    if isinstance(cursor, str):
        try:
            cursor = json.loads(cursor)
        except ValueError:
            frappe.throw("PAYG cursor must be a JSON [date, session] pair")
    return booking.available_sessions(student, course, cursor=cursor, limit=limit)


@frappe.whitelist()
def payg_preview_booking(student=None, session=None):
    return booking.preview_booking(student, session)


@frappe.whitelist()
def payg_confirm_booking(student=None, session=None, preview_card=None, request_key=None, confirmed_rules=False):
    confirmed = confirmed_rules is True or str(confirmed_rules).lower() in ("1", "true")
    result = booking.confirm_booking(student, session, preview_card, request_key,
                                     confirmed_rules=confirmed)
    return {"booking": result.name, "card": result.card, "status": result.status,
            "attendance_entry": result.attendance_entry}


@frappe.whitelist()
def payg_cancel_booking(booking_id=None):
    result = booking.cancel_booking(booking_id)
    return {"booking": result.name, "card": result.card, "status": result.status,
            "cancelled_at": result.cancelled_at}


@frappe.whitelist()
def payg_booking_history(student=None, limit=100):
    rows = booking.family_booking_history(student=student, limit=limit)
    return {"items": [_booking_payload(row) for row in rows]}
