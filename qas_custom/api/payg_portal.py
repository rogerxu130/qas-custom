"""Parent-facing PAYG adapters. Family identity is always resolved by the service."""
import json
import frappe

from qas_custom.modules.payg import booking
from qas_custom.services import payg_read_models


@frappe.whitelist()
def payg_family_cards(student=None):
    result = booking.family_cards(student=student)
    return {"students": result["students"],
            "cards": payg_read_models.enrich_cards(result["cards"])}


@frappe.whitelist()
def payg_available_sessions(student=None, course=None, cursor=None, limit=30):
    if isinstance(cursor, str):
        try:
            cursor = json.loads(cursor)
        except ValueError:
            frappe.throw("PAYG cursor must be a JSON [date, session] pair")
    result = booking.available_sessions(student, course, cursor=cursor, limit=limit)
    return payg_read_models.enrich_available_sessions(result)


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
    return {"items": payg_read_models.enrich_booking_history(rows)}
