"""Overlay current invoice debt on the existing term attendance report."""
from collections import defaultdict
import json

import frappe
from frappe.utils import date_diff, flt, get_datetime_in_timezone


def debt_kind(invoice, payg=None):
    if invoice.get("qas_invoice_type") in {"Store Order", "Material Order"} or invoice.get("source_doctype") == "Store Order":
        return ["Art supplies"]
    if payg or invoice.get("qas_invoice_type") == "PAYG Card":
        if payg is not None and "non_payg_debt_types" in payg:
            return sorted(set(["Ten-class pass"] + payg["non_payg_debt_types"]))
        return ["Ten-class pass"] if invoice.get("qas_invoice_type") == "PAYG Card" else ["Course fees", "Ten-class pass"]
    return ["Course fees"] if invoice.get("enrollment") or invoice.get("term") or invoice.get("source_inquiry") or invoice.get("source_doctype") in {"Enrollment", "Inquiry"} else ["Other fees"]


def followup_invoice(invoice, payg, today):
    item = dict(invoice, payable_amount=max(0, flt(invoice.get("outstanding_amount"))))
    item["debt_types"] = debt_kind(invoice, payg)
    overdue = max(0, date_diff(today, invoice["due_date"])) if invoice.get("due_date") else 0
    booked = bool(payg and payg.get("booking_count"))
    item["bookings"] = payg.get("bookings", []) if payg else []
    item.update(overdue_days=overdue, cards=payg.get("cards", []) if payg else [], booking_count=payg.get("booking_count", 0) if payg else 0)
    item["reasons"] = (["Booked but unpaid"] if booked else []) + (["Overdue"] if overdue else [])
    if invoice.get("order_status") == "Dispatched":
        item["reasons"].append("Dispatched but unpaid")
    item["needs_followup"] = item["debt_types"] != ["Ten-class pass"] or booked or overdue > 0
    if not item["needs_followup"]:
        item["reasons"] = ["Within payment period"]
    return item


def build_family_followups(snapshot_rows, invoices, parents, payg_rows, payg_attendance, today):
    from qas_custom.services.school_admin_reporting import _attendance_classification, _attendance_counts
    families = {row["parent_record"]: dict(row, students=[dict(student) for student in row.get("students") or []], invoices=[], debt_types=[], debt_reasons=[], outstanding_amount=0, live_financials=True) for row in snapshot_rows}
    parent_map = {row["name"]: row for row in parents}
    by_customer = defaultdict(set)
    for parent in parents:
        if parent.get("customer"):
            by_customer[parent["customer"]].add(parent["name"])
    def family(parent):
        if parent not in families:
            detail = parent_map.get(parent, {})
            families[parent] = {"name": "live:" + parent, "parent_record": parent,
                "parent_name": detail.get("parent_name") or parent, "parent_email": detail.get("email") or detail.get("email_id") or "",
                "parent_phone": detail.get("mobile_number") or detail.get("phone") or "", "students": [], "invoices": [],
                "debt_types": [], "debt_reasons": [], "outstanding_amount": 0, "attendance_classification": "No attendance records", "live_financials": True}
        return families[parent]
    payg_map = {row["name"]: row for row in payg_rows}
    submitted = set()
    seen = set()
    for invoice in invoices:
        if invoice["name"] in seen:
            continue
        seen.add(invoice["name"])
        parent = invoice.get("parent")
        if not parent:
            matches = by_customer.get(invoice.get("customer"), set())
            parent = next(iter(matches)) if len(matches) == 1 else None
        if not parent:
            continue
        submitted.add(parent)
        if flt(invoice.get("outstanding_amount")) <= 0.005:
            continue
        item = followup_invoice(invoice, payg_map.get(invoice["name"]), today)
        target = family(parent)
        target["invoices"].append(item)
        students = {student["student"]: student for student in target["students"]}
        for booking in item["bookings"]:
            detail = students.setdefault(booking["student"], {"student": booking["student"], "student_name": booking.get("student_name") or booking["student"], "class_schedule": []})
            _add_schedule(detail, booking.get("schedule"))
        target["students"] = list(students.values())
        target["outstanding_amount"] += item["payable_amount"]
        target["debt_types"] = sorted(set(target["debt_types"] + item["debt_types"]))
        target["debt_reasons"] = sorted(set(target["debt_reasons"] + item["reasons"]))
    attendance_by_parent = defaultdict(list)
    for attendance in payg_attendance:
        attendance_by_parent[attendance["parent"]].append(attendance)
    for parent, records in attendance_by_parent.items():
        target = family(parent)
        counts = _attendance_counts(records)
        for key, value in counts.items():
            target[key] = int(target.get(key) or 0) + value
        target["attendance_classification"] = _attendance_classification(target)
        by_student = defaultdict(list)
        for record in records:
            by_student[record["student"]].append(record)
        students = {row["student"]: row for row in target["students"]}
        for student, student_records in by_student.items():
            detail = students.setdefault(student, {"student": student, "student_name": student_records[0].get("student_name") or student, "class_schedule": []})
            for key, value in _attendance_counts(student_records).items():
                detail[key] = int(detail.get(key) or 0) + value
            detail["attendance_classification"] = _attendance_classification(detail)
            for record in student_records:
                _add_schedule(detail, record)
        target["students"] = list(students.values())
    for parent, target in families.items():
        if any(row["needs_followup"] for row in target["invoices"]):
            target["invoice_classification"] = "Outstanding"
        elif target["invoices"]:
            target["invoice_classification"] = "Not Due"
        elif parent in submitted or target.get("invoice_classification") == "Outstanding":
            target["invoice_classification"] = "Not Outstanding"
    return sorted(families.values(), key=lambda row: ("Booked but unpaid" not in row["debt_reasons"], (row.get("parent_name") or row["parent_record"]).lower()))


def _add_schedule(student, record):
    if record and record.get("day_of_week"):
        schedule = {"day_of_week": record["day_of_week"], "course": record.get("course") or ""}
        schedules = student.setdefault("class_schedule", [])
        if schedule not in schedules:
            schedules.append(schedule)


def collect_family_followups(snapshot_rows, term):
    from qas_custom.services.school_admin import _safe_fields
    from qas_custom.services.payg_payment_followups import collect_payg_payment_followups
    from qas_custom.services.order_invoices import financials
    invoice_rows = frappe.get_all("Sales Invoice", filters={"docstatus": 1, "is_return": 0},
        fields=_safe_fields("Sales Invoice", ["name", "parent", "customer", "qas_invoice_type", "source_doctype", "source_document", "enrollment", "term", "source_inquiry", "outstanding_amount", "due_date", "status", "docstatus"]), limit_page_length=0)
    # Order balances include revisions/credits. Count each order once using its existing billing calculation.
    orders_seen, invoices = set(), []
    for row in invoice_rows:
        if debt_kind(row) == ["Art supplies"] and row.get("source_doctype") == "Store Order":
            if row.source_document in orders_seen:
                continue
            orders_seen.add(row.source_document)
            order = frappe.get_doc("Store Order", row.source_document)
            state = financials(order)
            row.update(name=order.invoice or row.name, outstanding_amount=state["remaining_amount"], order_status="Dispatched" if order.status == "Collected" else order.status)
            if order.status == "Cancelled":
                continue
        invoices.append(row)
    parents = frappe.get_all("Parent", fields=_safe_fields("Parent", ["name", "customer", "parent_name", "email", "email_id", "mobile_number", "phone"]), limit_page_length=0)
    now = get_datetime_in_timezone("Australia/Brisbane")
    attendance = frappe.db.sql("""SELECT a.name,a.student,a.status,p.name AS parent,st.student_name,w.day_of_week,w.course
        FROM `tabClass Attendance Entry` a JOIN `tabStudent` st ON st.name=a.student
        JOIN `tabParent` p ON p.name=st.guardian
        JOIN `tabCourse Sessions` s ON s.name=a.course_session
        JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
        WHERE a.source_doctype='QAS PAYG Booking' AND w.term=%s AND s.status!='Cancelled'
        AND TIMESTAMP(s.session_date,COALESCE(w.end_time,w.start_time))<=%s""", (term, now), as_dict=True)
    return build_family_followups(snapshot_rows, invoices, parents, collect_payg_payment_followups(), attendance, now.date())


def filter_family_followups(rows, attendance=None, invoice=None, query=None, day_of_week=None, debt_type=None):
    terms = str(query or "").lower().split()
    def matches(row):
        if attendance and (not row.get("to_be_started_count") if attendance == "To be started" else row.get("attendance_classification") != attendance):
            return False
        if invoice and row.get("invoice_classification") != invoice:
            return False
        if debt_type and debt_type not in row["debt_types"]:
            return False
        if day_of_week and not any(schedule.get("day_of_week") == day_of_week for student in row.get("students", []) for schedule in student.get("class_schedule", [])):
            return False
        text = json.dumps(row, ensure_ascii=False, default=str).lower()
        return all(term in text for term in terms)
    return [row for row in rows if matches(row)]
