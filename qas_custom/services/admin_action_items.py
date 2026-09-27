"""School Admin work queues, derived from source records rather than tick boxes."""
import frappe
from frappe.utils import cint, get_url_to_form
from qas_custom.services.admin_followups import brisbane_now, require_admin

INQUIRY_FIELDS = ["name", "status", "inquiry_type", "submitted_student_name", "student",
                  "contact_name", "campus", "preferred_course", "requested_start_date",
                  "current_appointment_date", "current_appointment_time", "review_reason", "creation"]


def queue_definitions():
    today = str(brisbane_now().date())
    unconverted = {"converted_enrollment": ["is", "not set"], "converted_trial_inquiry": ["is", "not set"]}
    return {
        "enrollment": ("Inquiry", {"inquiry_type": "Direct Enrollment", "status": ["in", ["New", "Planned", "Needs Review"]], **unconverted}, INQUIRY_FIELDS),
        "scheduling": ("Inquiry", {"inquiry_type": ["in", ["Trial Lesson", "School Visit"]], "status": ["in", ["New", "Needs Review"]], **unconverted}, INQUIRY_FIELDS),
        "attendance": ("Inquiry", {"inquiry_type": ["in", ["Trial Lesson", "School Visit"]], "status": ["in", ["Booked", "Rescheduled"]], "current_appointment_date": ["<", today], **unconverted}, INQUIRY_FIELDS),
        "follow_up": ("Inquiry", {"inquiry_type": ["in", ["Trial Lesson", "School Visit"]], "status": ["in", ["Completed", "Follow-up", "No-show"]], **unconverted}, INQUIRY_FIELDS),
        "draft_invoice": ("Sales Invoice", {"docstatus": 0, "status": ["!=", "Cancelled"]}, ["name", "customer_name", "grand_total", "currency", "status", "creation"]),
        "overdue_invoice": ("Sales Invoice", {"docstatus": 1, "outstanding_amount": [">", 0], "due_date": ["<", today], "is_return": 0}, ["name", "customer_name", "outstanding_amount", "currency", "due_date", "status", "creation"]),
        "payment_review": ("Payment Collection Request", {"status": "Pending Review"}, ["name", "parent", "campus", "request_type", "collected_amount", "invoice", "status", "creation"]),
        "store_order": ("Store Order", {"status": ["in", ["Ordered", "Ready for collection"]]}, ["name", "parent", "pickup_campus", "status", "creation"]),
    }


def get_action_items(kind="enrollment", start=0, limit=30):
    require_admin()
    definitions = queue_definitions()
    if kind not in definitions:
        frappe.throw("Unknown action item category.")
    start, limit = max(0, cint(start)), min(100, max(1, cint(limit or 30)))
    counts = {key: frappe.db.count(doctype, filters=filters)
              for key, (doctype, filters, _) in definitions.items()}
    doctype, filters, fields = definitions[kind]
    rows = frappe.get_all(doctype, filters=filters, fields=fields,
                          order_by="creation asc, name asc", limit_start=start, limit_page_length=limit)
    if kind == "payment_review":
        for row in rows:
            row["review_url"] = get_url_to_form(doctype, row["name"])
    return {"kind": kind, "counts": counts, "items": rows, "total": counts[kind],
            "start": start, "has_more": start + len(rows) < counts[kind]}
