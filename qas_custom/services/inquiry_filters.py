"""Appointment time ranges are independent of inquiry lifecycle status."""
import frappe
from frappe import _
from frappe.utils import add_days, getdate, get_datetime_in_timezone


def inquiry_queue_filters(queue, status=None, reference_date=None):
    reference_date = getdate(reference_date or get_datetime_in_timezone("Australia/Brisbane"))
    if queue == "upcoming":
        return {"current_appointment_date": [">=", reference_date]}
    if queue in {"post_visit", "post_trial"}:
        return {"current_appointment_date": ["<", reference_date]}
    # Compatibility for older clients, which still send lifecycle queues.
    if queue in {"parked", "needs_scheduling"}:
        expected = "Parked" if queue == "parked" else "Needs Review"
        return {"status": expected} if not status or status == expected else {"name": "__qas_no_matching_inquiry__"}
    return {}


def inquiry_date_filter(queue_filter=None, *, from_date=None, to_date=None):
    start_date = getdate(from_date) if from_date else None
    end_date = getdate(to_date) if to_date else None
    if start_date and end_date and start_date > end_date:
        frappe.throw(_("From date cannot be later than To date."))
    if queue_filter:
        operator, value = queue_filter
        value = getdate(value)
        if operator == ">=":
            start_date = max(filter(None, [start_date, value]))
        elif operator == "<":
            end_date = min(filter(None, [end_date, add_days(value, -1)]))
    if start_date and end_date and start_date > end_date:
        return False
    if start_date and end_date:
        return ["between", [start_date, end_date]]
    if start_date:
        return [">=", start_date]
    if end_date:
        return ["<=", end_date]
    return None


def inquiry_status_filter(status):
    if status == "Booked":
        return ["in", ["Booked", "Rescheduled"]]
    if status == "Needs Review":
        return ["in", ["Needs Review", "Planned"]]
    return status
