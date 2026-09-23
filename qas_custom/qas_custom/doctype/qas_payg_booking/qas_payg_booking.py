import frappe
from frappe.model.document import Document
from qas_custom.qas_custom.doctype.payg_validation import require_equal

class QASPAYGBooking(Document):
    def validate(self):
        if self.is_new():
            if self.status != "Reserved":
                frappe.throw("New PAYG booking must start Reserved")
        else:
            before = self.get_doc_before_save()
            if not before:
                frappe.throw("Existing PAYG booking is required")
            for field in ("family_parent", "student", "card", "course_session",
                          "course_snapshot", "card_expires_on_snapshot", "request_key",
                          "cancellable_until"):
                if str(self.get(field) or "") != str(before.get(field) or ""):
                    frappe.throw(f"PAYG booking {field} cannot change")
            if before.attendance_entry and self.attendance_entry != before.attendance_entry:
                frappe.throw("PAYG booking attendance entry can only be set once")
            transitions = {
                "Reserved": {"Reserved", "Locked", "Cancelled"},
                "Locked": {"Locked", "Completed", "Cancelled"},
                "Completed": {"Completed", "Cancelled"},
                "Cancelled": {"Cancelled"},
            }
            if self.status not in transitions.get(before.status, set()):
                frappe.throw("PAYG booking status transition is invalid")
            if before.status == "Cancelled":
                for field in ("cancelled_at", "cancelled_by", "cancel_reason"):
                    if self.get(field) != before.get(field):
                        frappe.throw(f"PAYG booking {field} cannot change after cancellation")
        if self.status == "Cancelled":
            if not all(self.get(field) for field in ("cancelled_at", "cancelled_by", "cancel_reason")):
                frappe.throw("PAYG cancellation requires time, actor, and reason")
        elif any(self.get(field) for field in ("cancelled_at", "cancelled_by", "cancel_reason")):
            frappe.throw("PAYG cancellation audit requires Cancelled status")
        card = frappe.db.get_value("QAS PAYG Card", self.card, ("family_parent", "course", "expires_on"), as_dict=True)
        if not card:
            frappe.throw("PAYG card is required")
        require_equal(self, "family_parent", card.family_parent)
        require_equal(self, "course_snapshot", card.course)
        if str(self.card_expires_on_snapshot) != str(card.expires_on) and self.is_new():
            frappe.throw("Booking card expiry snapshot must match original card")
        require_equal(self, "family_parent", frappe.db.get_value("Student", self.student, "guardian"))
        if self.is_new():
            timeslot = frappe.db.get_value("Course Sessions", self.course_session, "weekly_timeslot")
            require_equal(self, "course_snapshot", frappe.db.get_value("Weekly Timeslot", timeslot, "course"))
        if self.attendance_entry:
            attendance = frappe.db.get_value(
                "Class Attendance Entry", self.attendance_entry,
                ("student", "course_session", "source_doctype", "source_document"), as_dict=True)
            if not attendance or (attendance.student, attendance.course_session,
                                  attendance.source_doctype, attendance.source_document) != (
                                      self.student, self.course_session, "QAS PAYG Booking", self.name):
                frappe.throw("Attendance must reference this PAYG booking and student")
        if self.status in ("Locked", "Completed") and not frappe.db.exists(
                "QAS PAYG Entry", {"booking": self.name, "kind": "Consume"}):
            frappe.throw("PAYG booking must be consumed before locking or completion")
        if self.status == "Cancelled" and not frappe.db.exists(
                "QAS PAYG Entry", {"booking": self.name, "kind": "Return"}):
            frappe.throw("PAYG booking must return its session before cancellation")
