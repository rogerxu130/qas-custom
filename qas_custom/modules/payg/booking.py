"""Family PAYG discovery, reservation and early cancellation.

All commands join the caller's transaction. A savepoint protects each multi-row write.
"""
from datetime import date, datetime, timedelta
from uuid import uuid4

import frappe
from frappe.utils import getdate, get_time, get_datetime_in_timezone

from qas_custom.modules.course_schedule import session_resources
from qas_custom.modules.payg.rules import (as_brisbane_datetime, can_book, can_parent_cancel,
                                            choose_card, confirm_preview_card)
from qas_custom.services.adhoc_booking import require_parent
from qas_custom.qas_custom.doctype.qas_payg_booking.qas_payg_booking import QASPAYGBooking
from qas_custom.services.support_view import get_support_view_token


BOOKING = "QAS PAYG Booking"
CARD = "QAS PAYG Card"
ENTRY = "QAS PAYG Entry"


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _family(parent=None, *, write=False):
    if write and get_support_view_token():
        frappe.throw("Support View cannot write PAYG bookings", frappe.PermissionError)
    own = require_parent()
    if parent and own.name != parent:
        frappe.throw("Another family's PAYG data is unavailable", frappe.PermissionError)
    return own


def _student(student, family, lock=False):
    if not student:
        frappe.throw("Student is required")
    if lock:
        rows = frappe.db.sql("""SELECT name, guardian, status, date_of_birth
            FROM `tabStudent` WHERE name=%s FOR UPDATE""", (student,), as_dict=True)
        if not rows:
            frappe.throw("Student was not found")
        row = rows[0]
    else:
        row = frappe.get_doc("Student", student)
    if row.guardian != family:
        frappe.throw("Student does not belong to this family", frappe.PermissionError)
    return row


def _cards(family, course=None, lock=False):
    filters = {"family_parent": family}
    if course:
        filters["course"] = course
    if lock:
        # Parent is already locked by confirm; this locking read sees cards issued
        # while that Parent lock was awaited, even under MariaDB REPEATABLE READ.
        if course:
            rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Card`
                WHERE family_parent=%s AND course=%s ORDER BY name FOR UPDATE""",
                (family, course), as_dict=True)
        else:
            rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Card`
                WHERE family_parent=%s ORDER BY name FOR UPDATE""", (family,), as_dict=True)
        names = sorted(row.name for row in rows)
    else:
        names = sorted(frappe.get_all(CARD, filters=filters, pluck="name"))
    return [frappe.get_doc(CARD, name, for_update=lock) for name in names]


def _query_limit(value, maximum):
    try:
        count = int(value)
    except (TypeError, ValueError):
        frappe.throw("PAYG limit must be an integer")
    if count < 1:
        frappe.throw("PAYG limit must be positive")
    return min(count, maximum)


def _start(session, slot):
    if not session.session_date or not slot.start_time:
        frappe.throw("Session date and start time are required")
    return as_brisbane_datetime(datetime.combine(getdate(session.session_date), get_time(slot.start_time)))


def _age_reason(student, course, session_date):
    # Course/Student have age fields but no canonical level relationship;
    # do not infer a class level from course names or unrelated records.
    birth = student.get("date_of_birth")
    if not birth:
        return "student_age_unknown"
    day, born = getdate(session_date), getdate(birth)
    age = day.year - born.year - ((day.month, day.day) < (born.month, born.day))
    if course.get("min_age") and age < int(course.min_age):
        return "below_minimum_age"
    if course.get("max_age") and age > int(course.max_age):
        return "above_maximum_age"
    return None


def _basic_reasons(student, session, slot, term, course, now):
    reasons = []
    if student.status != "Active": reasons.append("student_inactive")
    if course.status != "Active": reasons.append("course_inactive")
    if session.status != "Scheduled": reasons.append("session_not_scheduled")
    if slot.status != "Active" or term.status not in ("Upcoming", "Active"):
        reasons.append("class_or_term_closed")
    if not term.start_date or not term.end_date or not (getdate(term.start_date) <= getdate(session.session_date) <= getdate(term.end_date)):
        reasons.append("outside_term")
    if not slot.get("teacher_override") and not session.get("teacher_override") and not slot.teacher:
        reasons.append("teacher_missing")
    if not slot.start_time or not slot.end_time:
        reasons.append("time_missing")
    elif not can_book(now=now, session_start=_start(session, slot)):
        reasons.append("inside_72_hours")
    if getdate(session.session_date).strftime("%A") != slot.day_of_week:
        reasons.append("weekday_mismatch")
    age = _age_reason(student, course, session.session_date)
    if age: reasons.append(age)
    return reasons


def _seat_reasons(student, session, slot):
    reasons = []
    if session_resources.student_has_conflict(student, session, slot):
        reasons.append("student_conflict")
    capacity = session_resources.effective_capacity(slot)
    active = session_resources.active_rows(session.name)
    enrolled = frappe.db.sql("""SELECT student, enrollment_type FROM `tabEnrollment`
        WHERE weekly_timeslot=%s AND status IN ('Planned','Active')""",
        (slot.name,), as_dict=True)
    if any(row.student == student for row in enrolled):
        reasons.append("student_enrolled")
    occupied = {row.student for row in active} | {
        row.student for row in enrolled if row.enrollment_type == "Full-Term"}
    if capacity <= 0 or len(occupied) >= capacity:
        reasons.append("full")
    return reasons


def family_cards(student=None, parent=None):
    family = _family(parent)
    students = frappe.get_all("Student", filters={"guardian": family.name},
                              fields=["name", "student_name", "status", "age"])
    if student:
        _student(student, family.name)
    return {"students": students, "cards": _cards(family.name)}


def family_booking_history(student=None, limit=100):
    family = _family()
    if student:
        _student(student, family.name)
    count = _query_limit(limit, 200)
    filters = {"family_parent": family.name}
    if student:
        filters["student"] = student
    return frappe.get_all(BOOKING, filters=filters,
                          fields=["name", "student", "card", "course_session", "attendance_entry",
                                  "status", "cancellable_until", "cancelled_at"],
                          order_by="creation desc", limit_page_length=count)


def available_sessions(student, course, cursor=None, limit=30, parent=None):
    """Page all open terms using the stable (session_date, name) cursor."""
    family = _family(parent)
    pupil = _student(student, family.name)
    if not course:
        frappe.throw("Course is required")
    limit = _query_limit(limit, 100)
    course_doc = frappe.get_doc("Course", course)
    if cursor is not None:
        if (not isinstance(cursor, (tuple, list)) or len(cursor) != 2 or
                not isinstance(cursor[0], str) or not isinstance(cursor[1], str) or not cursor[1]):
            frappe.throw("PAYG cursor must be a [session_date, session] pair")
        try:
            date.fromisoformat(cursor[0])
        except (TypeError, ValueError):
            frappe.throw("PAYG cursor date is invalid")
    else:
        cursor = ("1000-01-01", "")
    # Scan raw cursor batches until we have a full page of age-matched sessions.
    # Unknown birth dates remain visible with an explicit unbookable reason.
    candidates = []
    scan_cursor = cursor
    while len(candidates) <= limit:
        rows = frappe.db.sql("""SELECT s.name, s.session_date FROM `tabCourse Sessions` s
            JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
            JOIN `tabTerm` t ON t.name=w.term
            WHERE w.course=%s AND s.status='Scheduled' AND w.status='Active'
              AND t.status IN ('Upcoming','Active')
              AND (s.session_date>%s OR (s.session_date=%s AND s.name>%s))
            ORDER BY s.session_date, s.name LIMIT %s""",
            (course, scan_cursor[0], scan_cursor[0], scan_cursor[1], limit + 1), as_dict=True)
        if not rows:
            break
        last = (str(rows[-1].session_date), rows[-1].name)
        if last <= (str(scan_cursor[0]), scan_cursor[1]):
            break
        for row in rows:
            reason = _age_reason(pupil, course_doc, row.session_date)
            if reason not in ("below_minimum_age", "above_maximum_age"):
                candidates.append(row)
                if len(candidates) > limit:
                    break
        scan_cursor = last
        if len(rows) < limit + 1:
            break
    cards = _cards(family.name, course)
    now = _now()
    items = []
    for row in candidates[:limit]:
        session = frappe.get_doc("Course Sessions", row.name)
        slot = frappe.get_doc("Weekly Timeslot", session.weekly_timeslot)
        term = frappe.get_doc("Term", slot.term)
        reasons = _basic_reasons(pupil, session, slot, term, course_doc, now)
        selected = choose_card(cards, now=now, session_start=_start(session, slot)) if slot.start_time else None
        if not selected: reasons.append("no_eligible_card")
        if slot.start_time and slot.end_time:
            reasons.extend(_seat_reasons(student, session, slot))
        items.append({"name": session.name, "session_date": session.session_date,
                      "card": selected.name if selected else None, "bookable": not reasons,
                      "reasons": reasons})
    return {"items": items, "has_more": len(candidates) > limit,
            "next_cursor": (str(items[-1]["session_date"]), items[-1]["name"]) if len(candidates) > limit and items else None}


def preview_booking(student, session, parent=None):
    family = _family(parent)
    pupil = _student(student, family.name)
    occurrence = frappe.get_doc("Course Sessions", session)
    slot = frappe.get_doc("Weekly Timeslot", occurrence.weekly_timeslot)
    term = frappe.get_doc("Term", slot.term)
    course = frappe.get_doc("Course", slot.course)
    now = _now()
    reasons = _basic_reasons(pupil, occurrence, slot, term, course, now)
    card = choose_card(_cards(family.name, slot.course), now=now,
                       session_start=_start(occurrence, slot)) if slot.start_time else None
    if not card: reasons.append("no_eligible_card")
    if slot.start_time and slot.end_time:
        reasons.extend(_seat_reasons(student, occurrence, slot))
    return {"card": card.name if card else None, "bookable": not reasons, "reasons": reasons,
            "session": session, "student": student}


def _booking_by_request_key(request_key, *, after_duplicate=False):
    if after_duplicate:
        # Only a proven existing unique-key winner is read with a row lock.
        rows = frappe.db.sql("""SELECT name FROM `tabQAS PAYG Booking`
            WHERE request_key=%s FOR UPDATE""", (request_key,), as_dict=True)
        name = rows[0].name if rows else None
    else:
        # A missing-key FOR UPDATE takes a gap lock under MariaDB RR.
        name = frappe.db.get_value(BOOKING, {"request_key": request_key}, "name")
    return frappe.get_doc(BOOKING, name, for_update=True) if name else None


def _same_booking(booking, family, student, session):
    if (booking.family_parent, booking.student, booking.course_session) != (family, student, session):
        frappe.throw("Booking request key belongs to another reservation")
    return booking


def confirm_booking(student, session, preview_card, request_key, *, confirmed_rules=False, parent=None):
    if confirmed_rules is not True:
        frappe.throw("Please confirm the PAYG booking rules before reserving")
    family = _family(parent, write=True)
    if not all((student, session, preview_card, request_key)):
        frappe.throw("Student, session, preview card and request key are required")
    savepoint = "payg_confirm_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    booking_insert_conflict = False
    try:
        pupil = _student(student, family.name, lock=True)
        existing = _booking_by_request_key(request_key)
        if existing:
            return _same_booking(existing, family.name, student, session)
        # Serialize candidate-card discovery with issue actions for this family.
        frappe.db.sql("SELECT name FROM `tabParent` WHERE name=%s FOR UPDATE", (family.name,))
        # Resolve identifiers without locking Session, then lock all family cards by stable name.
        session_id = frappe.db.get_value("Course Sessions", session, "weekly_timeslot")
        course_id = frappe.db.get_value("Weekly Timeslot", session_id, "course") if session_id else None
        if not course_id:
            frappe.throw("Course session was not found")
        cards = _cards(family.name, course_id, lock=True)
        now = _now()
        def validate(occurrence, slot, term, _rows):
            if slot.course != course_id or _start(occurrence, slot) != start:
                frappe.throw("Session changed; refresh preview")
            course = frappe.get_doc("Course", course_id)
            reasons = _basic_reasons(pupil, occurrence, slot, term, course, now)
            if reasons:
                frappe.throw("Session is unavailable: " + ", ".join(reasons))
            selected = confirm_preview_card(cards, now=now,
                                            session_start=_start(occurrence, slot),
                                            preview_card=preview_card)
            if selected.name != booking.card:
                frappe.throw("Card changed; refresh preview")
        # Booking must exist before its Reserve Entry; shared reservation runs after the card lock.
        occurrence = frappe.db.get_value("Course Sessions", session, ["session_date", "weekly_timeslot"], as_dict=True)
        slot_id = occurrence.weekly_timeslot if occurrence else None
        slot_start = frappe.db.get_value("Weekly Timeslot", slot_id, "start_time") if slot_id else None
        if not slot_start:
            frappe.throw("Session start is unavailable")
        start = as_brisbane_datetime(datetime.combine(getdate(occurrence.session_date), get_time(slot_start)))
        selected = confirm_preview_card(cards, now=now, session_start=start, preview_card=preview_card)
        booking = frappe.get_doc({"doctype": BOOKING, "family_parent": family.name,
                                  "student": student, "card": selected.name, "course_session": session,
                                  "course_snapshot": course_id, "card_expires_on_snapshot": selected.expires_on,
                                  "status": "Reserved", "cancellable_until": start - timedelta(hours=72),
                                  "request_key": request_key})
        booking.flags.payg_create_context = {
            "token": QASPAYGBooking._SERVICE_CREATE_TOKEN,
            "family_parent": pupil.guardian, "student": pupil.name,
            "card": selected.name, "card_family": selected.family_parent,
            "card_course": selected.course, "course_session": session,
            "course_snapshot": course_id,
            "card_expires_on_snapshot": selected.expires_on,
            "request_key": request_key, "cancellable_until": start - timedelta(hours=72),
        }
        try:
            # Frappe checks Links before autoname. Locked service context is the
            # authority here; a stale RR snapshot can miss a just-issued card.
            booking.insert(ignore_permissions=True, ignore_links=True)
        except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
            booking_insert_conflict = True
            raise
        finally:
            booking.flags.payg_create_context = None
        attendance = session_resources.reserve_regular_place(
            student, session, BOOKING, booking.name, "Pay-as-you-go", validate_business=validate)
        frappe.get_doc({"doctype": ENTRY, "card": selected.name, "booking": booking.name,
                        "kind": "Reserve", "available_delta": -1, "reserved_delta": 1,
                        "consumed_delta": 0, "operation_key": f"reserve:{booking.name}",
                        "actor": frappe.session.user, "occurred_at": now}).insert(
                            ignore_permissions=True, ignore_links=True)
        booking.attendance_entry = attendance
        booking.flags.payg_mutation_token = QASPAYGBooking._SERVICE_MUTATION_TOKEN
        try:
            booking.save(ignore_permissions=True, ignore_links=True)
        finally:
            booking.flags.payg_mutation_token = None
            booking.flags.ignore_links = False
        return booking
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=savepoint)
        if booking_insert_conflict:
            _student(student, family.name, lock=True)
            existing = _booking_by_request_key(request_key, after_duplicate=True)
            if existing:
                return _same_booking(existing, family.name, student, session)
        raise
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def cancel_booking(booking_id, parent=None):
    family = _family(parent, write=True)
    if not booking_id:
        frappe.throw("Booking is required")
    # Only read the Student identifier before locking it.
    student = frappe.db.get_value(BOOKING, booking_id, "student")
    if not student:
        frappe.throw("Booking does not exist")
    savepoint = "payg_cancel_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        _student(student, family.name, lock=True)
        booking = frappe.get_doc(BOOKING, booking_id, for_update=True)
        if booking.family_parent != family.name or booking.student != student:
            frappe.throw("Booking does not belong to this family", frappe.PermissionError)
        if booking.status == "Cancelled":
            return booking
        card = frappe.get_doc(CARD, booking.card, for_update=True)
        occurrence = frappe.get_doc("Course Sessions", booking.course_session, for_update=True)
        slot = frappe.get_doc("Weekly Timeslot", occurrence.weekly_timeslot, for_update=True)
        frappe.get_doc("Term", slot.term, for_update=True)
        if slot.classroom:
            frappe.get_doc("Classroom", slot.classroom, for_update=True)
        rows = session_resources.active_rows(occurrence.name, lock=True)
        now = _now()
        if not can_parent_cancel(now=now, session_start=_start(occurrence, slot), booking_status=booking.status):
            frappe.throw("Parent cancellation requires more than 72 hours notice")
        if not booking.attendance_entry or not any(row.name == booking.attendance_entry for row in rows):
            frappe.throw("Active booking attendance was not found")
        was_consumed = bool(frappe.db.exists(ENTRY, {"booking": booking.name, "kind": "Consume"}))
        frappe.get_doc({"doctype": ENTRY, "card": card.name, "booking": booking.name,
                        "kind": "Return", "available_delta": 1,
                        "reserved_delta": 0 if was_consumed else -1,
                        "consumed_delta": -1 if was_consumed else 0,
                        "operation_key": f"return:{booking.name}",
                        "actor": frappe.session.user, "occurred_at": now}).insert(ignore_permissions=True)
        booking.status = "Cancelled"
        booking.cancelled_at = now
        booking.cancelled_by = frappe.session.user
        booking.cancel_reason = "Parent cancellation"
        booking.flags.payg_mutation_token = QASPAYGBooking._SERVICE_MUTATION_TOKEN
        try:
            booking.save(ignore_permissions=True)
        finally:
            booking.flags.payg_mutation_token = None
        frappe.db.set_value("Class Attendance Entry", booking.attendance_entry, "status", "Cancelled")
        return booking
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
