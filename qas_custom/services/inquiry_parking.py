from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

PARKABLE_STATUSES = {"Planned", "New", "Needs Review", "Booked", "Rescheduled", "Completed", "Follow-up", "No-show", "Further Trial Booked"}
SETTINGS = "QAS Inquiry Settings"


def validate_wait_days(value):
    try:
        days = int(str(value))
    except (ValueError, TypeError):
        frappe.throw(_("Parked waiting days must be a positive whole number."))
    if days < 1 or days > 3650:
        frappe.throw(_("Parked waiting days must be between 1 and 3650."))
    return days


def get_wait_days():
    value = frappe.db.get_single_value(SETTINGS, "parked_wait_days")
    # Frappe casts an unset Int singleton field to 0 before its first save.
    return validate_wait_days(value or 30)


def parking_summary(doc):
    days = get_wait_days()
    started = doc.get("parked_last_activity_at") or doc.get("parked_at")
    return {
        "wait_days": days,
        "parked_at": str(doc.get("parked_at") or ""),
        "last_activity_at": str(started or ""),
        "closes_at": str(get_datetime(started) + timedelta(days=days)) if started and doc.status == "Parked" else None,
        "previous_status": doc.get("parked_previous_status"),
    }


def lock_inquiry(name):
    if not name:
        frappe.throw(_("Inquiry is required."))
    return frappe.get_doc("Inquiry", name, for_update=True)


def add_parking_note(doc, note, *, system=True):
    frappe.get_doc({
        "doctype": "Inquiry Note", "inquiry": doc.name, "student": doc.get("student"),
        "note": note, "author": frappe.session.user, "edited_at": now_datetime(),
        "note_type": "System" if system else "Manual",
    }).insert(ignore_permissions=True)


def validate_parking_transition(doc):
    old = doc.get_doc_before_save()
    if doc.status != "Parked" or (old and old.status == "Parked"):
        return
    if not old or old.status not in PARKABLE_STATUSES or doc.get("converted_enrollment") or doc.get("converted_trial_inquiry"):
        frappe.throw(_("Only unresolved inquiries can be parked."))
    doc.parked_previous_status = old.status
    doc.parked_at = now_datetime()
    doc.parked_last_activity_at = doc.parked_at


def change_parking(inquiry, action, note=None):
    doc = lock_inquiry(inquiry)
    if action == "park":
        if doc.status != "Parked":
            if doc.status not in PARKABLE_STATUSES:
                frappe.throw(_("This inquiry cannot be parked from its current status."))
            doc.status = "Parked"
            doc.save(ignore_permissions=True)
            add_parking_note(doc, _("Inquiry parked. The global inactivity period is {0} days.").format(get_wait_days()))
    elif action == "resume":
        if doc.status != "Parked":
            frappe.throw(_("Only parked inquiries can be resumed."))
        previous = doc.get("parked_previous_status")
        if previous == "Rescheduled":
            previous = "Booked"
        doc.status = previous if previous in PARKABLE_STATUSES else "Needs Review"
        doc.save(ignore_permissions=True)
        add_parking_note(doc, _("Inquiry resumed from Parked. Status: {0}.").format(doc.status))
    elif action == "progress":
        if doc.status != "Parked":
            frappe.throw(_("Only parked inquiries can record Parked progress."))
        note = str(note or "").strip()
        if not note:
            frappe.throw(_("Describe the new communication or progress."))
        doc.parked_last_activity_at = now_datetime()
        doc.save(ignore_permissions=True)
        add_parking_note(doc, _("Parked progress (inactivity timer restarted): {0}").format(note), system=False)
    else:
        frappe.throw(_("Unsupported parking action."))
    from qas_custom.services.inquiry import build_inquiry_detail
    return build_inquiry_detail(doc.name)


def close_expired_parked_inquiries():
    days = get_wait_days()
    cutoff = now_datetime() - timedelta(days=days)
    names = frappe.get_all("Inquiry", filters={"status": "Parked", "parked_last_activity_at": ["<=", cutoff]}, pluck="name")
    for name in names:
        try:
            doc = lock_inquiry(name)
            activity = doc.get("parked_last_activity_at") or doc.get("parked_at")
            # Recheck under the same row lock as progress/resume to avoid stale closure.
            if doc.status == "Parked" and activity and get_datetime(activity) <= cutoff:
                if not doc.get("converted_enrollment") and not doc.get("converted_trial_inquiry"):
                    doc.status = "Inactive"
                    doc.inactive_reason = _("Automatically closed after {0} days in Parked without new progress.").format(days)
                    doc.save(ignore_permissions=True)
                    add_parking_note(doc, doc.inactive_reason)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Parked inquiry auto-close failed", message=frappe.get_traceback())
