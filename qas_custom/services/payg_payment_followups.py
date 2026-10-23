"""Live ten-class pass debt and booking context shared by staff queues and reports."""
from collections import defaultdict

import frappe
from frappe.utils import date_diff, flt, get_datetime_in_timezone


def collect_payg_payment_followups():
    from qas_custom.services.school_admin import _doctype_available
    if not _doctype_available("QAS PAYG Operation"):
        return []
    # Some sites do not have the optional invoice-to-family custom field.
    parent_condition = "AND (IFNULL(i.parent,'')='' OR i.parent=o.family_parent)" if frappe.db.has_column("Sales Invoice", "parent") else ""
    rows = frappe.db.sql(f"""SELECT i.name, i.customer, i.customer_name,
        i.outstanding_amount, i.currency, i.due_date, i.status, o.family_parent AS parent,
        p.parent_name, o.card, o.target_card, o.operation_type, c.issued_on
        FROM `tabQAS PAYG Operation` o
        JOIN `tabSales Invoice` i ON i.name=o.invoice
        LEFT JOIN `tabParent` p ON p.name=o.family_parent
        LEFT JOIN `tabQAS PAYG Card` c ON c.name=IF(o.operation_type='Purchase',o.card,o.target_card)
        WHERE o.operation_type IN ('Purchase','Exchange') AND o.status!='Cancelled'
        AND i.docstatus=1 AND i.is_return=0 AND i.outstanding_amount>0.005
        AND i.customer=o.customer
        {parent_condition}
        ORDER BY i.due_date,i.name,o.name""", as_dict=True)
    if not rows:
        return []
    non_payg_types = defaultdict(set)
    for item in frappe.get_all("Sales Invoice Item", filters={"parent": ["in", sorted({row.name for row in rows})], "parenttype": "Sales Invoice"},
            fields=["parent", "qas_source_doctype", "course", "amount"], limit_page_length=0):
        if item.qas_source_doctype != "QAS PAYG Operation" and flt(item.amount) > 0.005:
            non_payg_types[item.parent].add("Course fees" if item.course or item.qas_source_doctype in {"Enrollment", "Inquiry"} else "Other fees")
    families = sorted({row.parent for row in rows})
    exchanges = frappe.get_all("QAS PAYG Operation", filters={"operation_type": "Exchange", "status": "Completed", "family_parent": ["in", families]},
        fields=["family_parent", "source_card", "target_card"], limit_page_length=0)
    descendants = defaultdict(set)
    for exchange in exchanges:
        if exchange.source_card and exchange.target_card:
            descendants[(exchange.family_parent, exchange.source_card)].add(exchange.target_card)
    invoice_cards = defaultdict(dict)
    for row in rows:
        root = row.card if row.operation_type == "Purchase" else row.target_card
        if not root:
            continue
        pending, related = [root], set()
        while pending:
            card = pending.pop()
            if card in related:
                continue
            related.add(card)
            pending.extend(descendants[(row.parent, card)] - related)
        invoice_cards[row.name][root] = {"name": root, "issued_on": row.issued_on, "related": related}
    card_ids = sorted({card for roots in invoice_cards.values() for root in roots.values() for card in root["related"]})
    bookings = frappe.get_all("QAS PAYG Booking", filters={"card": ["in", card_ids], "status": ["!=", "Cancelled"], "attendance_entry": ["is", "set"]},
        fields=["name", "card", "family_parent", "student", "course_session", "attendance_entry", "status"], limit_page_length=0) if card_ids else []
    session_ids = sorted({row.course_session for row in bookings})
    sessions = {row.name: row for row in frappe.get_all("Course Sessions", filters={"name": ["in", session_ids], "status": ["!=", "Cancelled"]},
        fields=["name", "session_date", "weekly_timeslot"], limit_page_length=0)} if session_ids else {}
    slot_ids = sorted({row.weekly_timeslot for row in sessions.values() if row.weekly_timeslot})
    slots = {row.name: row for row in frappe.get_all("Weekly Timeslot", filters={"name": ["in", slot_ids]},
        fields=["name", "day_of_week", "course"], limit_page_length=0)} if slot_ids else {}
    attendance_ids = sorted({row.attendance_entry for row in bookings})
    attendance = {row.name: row.status for row in frappe.get_all("Class Attendance Entry", filters={"name": ["in", attendance_ids]}, fields=["name", "status"], limit_page_length=0)} if attendance_ids else {}
    student_ids = sorted({row.student for row in bookings})
    students = {row.name: row.student_name for row in frappe.get_all("Student", filters={"name": ["in", student_ids]}, fields=["name", "student_name"], limit_page_length=0)} if student_ids else {}
    today = get_datetime_in_timezone("Australia/Brisbane").date()
    result = {}
    for row in rows:
        if row.name in result:
            continue
        item = dict(row)
        item.update(customer_name=row.parent_name or row.customer_name, cards=[], bookings=[], docstatus=1, non_payg_debt_types=sorted(non_payg_types[row.name]))
        seen = set()
        for root in invoice_cards[row.name].values():
            item["cards"].append({"name": root["name"], "issued_on": root["issued_on"], "days_since_issue": max(0, date_diff(today, root["issued_on"])) if root["issued_on"] else None})
            for booking in bookings:
                if booking.name in seen or booking.family_parent != row.parent or booking.card not in root["related"] or booking.course_session not in sessions:
                    continue
                seen.add(booking.name)
                item["bookings"].append({"name": booking.name, "student": booking.student, "student_name": students.get(booking.student) or booking.student, "card": booking.card,
                    "course_session": booking.course_session, "session_date": sessions[booking.course_session].session_date,
                    "attendance_status": attendance.get(booking.attendance_entry) or "To be started",
                    "schedule": dict(slots.get(sessions[booking.course_session].weekly_timeslot) or {})})
        item["booking_count"] = len(item["bookings"])
        item["overdue_days"] = max(0, date_diff(today, row.due_date)) if row.due_date else 0
        item["review_reason"] = "Ten-class pass booked with an unpaid invoice" if item["booking_count"] else "Ten-class pass invoice overdue"
        result[row.name] = item
    return list(result.values())


def unpaid_booking_warnings():
    return [row for row in collect_payg_payment_followups() if row["booking_count"]]
