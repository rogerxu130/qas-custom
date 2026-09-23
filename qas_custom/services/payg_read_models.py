"""Batch-built PAYG display data; callers enforce family or admin permissions."""
from datetime import datetime, timedelta

import frappe
from frappe.utils import getdate, get_time, get_datetime_in_timezone

from qas_custom.modules.payg.rules import as_brisbane_datetime, can_parent_cancel


def _rows(doctype, names, fields):
    if not names:
        return []
    return frappe.get_all(doctype, filters={"name": ["in", sorted(names)]}, fields=fields,
                          limit_page_length=0)


def _course_labels(names):
    return {row.name: row.course_name or row.name
            for row in _rows("Course", names, ["name", "course_name"])}


def _product_label(product, course_labels):
    course_label = course_labels.get(product.course, product.course)
    return f"{course_label} · {product.sessions_per_card or 10} sessions"


def product_payloads(products=None):
    if products is None:
        products = frappe.get_all("QAS PAYG Product",
                                  fields=["name", "course", "standard_card_price", "sessions_per_card", "enabled"],
                                  order_by="course asc", limit_page_length=0)
    labels = _course_labels({row.course for row in products if row.course})
    return [{"name": row.name, "course": row.course,
             "course_label": labels.get(row.course, row.course),
             "product_label": _product_label(row, labels),
             "standard_card_price": row.standard_card_price,
             "sessions_per_card": row.sessions_per_card, "enabled": row.enabled}
            for row in products]


def enrich_cards(cards, products=None):
    if not cards:
        return []
    product_ids = {card.product for card in cards if card.product}
    product_map = {row.name: row for row in
                   (products if products is not None else
                    _rows("QAS PAYG Product", product_ids,
                          ["name", "course", "standard_card_price", "sessions_per_card", "enabled"]))}
    course_ids = {card.course for card in cards if card.course}
    course_ids.update(row.course for row in product_map.values() if row.course)
    labels = _course_labels(course_ids)
    today = as_brisbane_datetime(get_datetime_in_timezone("Australia/Brisbane")).date()
    items = []
    for card in cards:
        expired = bool(card.expires_on and getdate(card.expires_on) < today)
        product = product_map.get(card.product)
        items.append({
            "name": card.name, "course": card.course,
            "course_label": labels.get(card.course, card.course),
            "product": card.product,
            "product_label": _product_label(product, labels) if product else card.product,
            "issued_on": card.issued_on, "expires_on": card.expires_on,
            "unit_price_snapshot": card.unit_price_snapshot,
            "status": "Expired" if expired and card.status == "Active" else card.status,
            "available_count": card.available_count, "reserved_count": card.reserved_count,
            "consumed_count": card.consumed_count,
            "bookable": card.status == "Active" and not expired and int(card.available_count or 0) > 0,
        })
    return items


def _session_context(session_ids):
    sessions = {row.name: row for row in _rows(
        "Course Sessions", session_ids, ["name", "session_date", "weekly_timeslot", "teacher_override"])}
    slots = {row.name: row for row in _rows(
        "Weekly Timeslot", {row.weekly_timeslot for row in sessions.values() if row.weekly_timeslot},
        ["name", "course", "term", "campus", "classroom", "teacher", "start_time", "end_time"])}
    labels = _course_labels({row.course for row in slots.values() if row.course})
    teacher_ids = {session.teacher_override or slots[session.weekly_timeslot].teacher
                   for session in sessions.values() if session.weekly_timeslot in slots}
    teachers = {row.name: row.teacher_name or row.name for row in
                _rows("Teacher", {name for name in teacher_ids if name}, ["name", "teacher_name"])}
    context = {}
    for name, session in sessions.items():
        slot = slots.get(session.weekly_timeslot)
        if not slot:
            continue
        teacher = session.teacher_override or slot.teacher
        context[name] = {
            "course": slot.course, "course_label": labels.get(slot.course, slot.course),
            "session_date": session.session_date,
            "start_time": str(slot.start_time) if slot.start_time is not None else None,
            "end_time": str(slot.end_time) if slot.end_time is not None else None,
            "weekly_timeslot": slot.name, "term": slot.term, "campus": slot.campus,
            "classroom": slot.classroom, "teacher": teacher,
            "teacher_label": teachers.get(teacher, teacher),
        }
    return context


def enrich_available_sessions(result):
    result = dict(result)
    items = result.get("items") or []
    context = _session_context({row["name"] for row in items})
    result["items"] = [{**row, **context.get(row["name"], {})} for row in items]
    return result


def enrich_booking_history(bookings, cards=None):
    if not bookings:
        return []
    context = _session_context({row.course_session for row in bookings if row.course_session})
    if cards is None:
        cards = _rows("QAS PAYG Card", {row.card for row in bookings if row.card},
                      ["name", "expires_on", "status"])
    card_map = {card.name: card for card in cards}
    now = as_brisbane_datetime(get_datetime_in_timezone("Australia/Brisbane"))
    items = []
    for row in bookings:
        schedule = context.get(row.course_session, {})
        deadline = None
        can_cancel = False
        if schedule.get("session_date") and schedule.get("start_time"):
            start = as_brisbane_datetime(datetime.combine(
                getdate(schedule["session_date"]), get_time(schedule["start_time"])))
            deadline = start - timedelta(hours=72)
            can_cancel = can_parent_cancel(now=now, session_start=start,
                                           booking_status=row.status)
        items.append({
            "name": row.name, "student": row.student, "card": row.card,
            "course_session": row.course_session, "attendance_entry": row.attendance_entry,
            "status": row.status, "cancellable_until": row.cancellable_until,
            "cancelled_at": row.cancelled_at,
            "card_expires_on": card_map[row.card].expires_on if row.card in card_map else None,
            "card_expires_on_snapshot": row.card_expires_on_snapshot,
            "card_status": card_map[row.card].status if row.card in card_map else None,
            "can_cancel": can_cancel, "cancel_deadline": str(deadline) if deadline else None,
            **schedule,
        })
    return items
