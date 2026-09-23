"""Choose the landing portal from the signed-in family's persisted history."""
import frappe

from qas_custom.services.adhoc_booking import require_parent


def get_parent_portal_context_data():
	parent = require_parent()
	family = parent.name
	has_payg = bool(
		frappe.db.exists("QAS PAYG Card", {"family_parent": family})
		or frappe.db.exists("QAS PAYG Operation", {"family_parent": family})
	)
	students = frappe.get_all("Student", filters={"guardian": family}, pluck="name")
	has_standard = bool(
		frappe.db.exists("Adhoc Booking", {"parent": family})
		or frappe.db.exists("Workshop Enrollment", {"parent": family})
		or frappe.db.exists("Store Order", {"parent": family})
		or frappe.db.exists("Enrollment", {
			"parent": family, "enrollment_type": ["!=", "Pay-as-you-go"],
		})
	)
	if students and not has_standard:
		has_standard = bool(frappe.db.exists("Enrollment", {
			"student": ["in", students], "enrollment_type": ["!=", "Pay-as-you-go"],
		}))
	customer = parent.get("customer") if hasattr(parent, "get") else getattr(parent, "customer", None)
	if customer and not has_standard:
		has_standard = bool(frappe.db.exists("Sales Invoice", {
			"customer": customer, "docstatus": 1,
			"qas_invoice_type": ["in", ["Course", "Other", "Workshop"]],
		}))
	return {
		"has_payg_access": has_payg,
		"has_standard_access": has_standard,
		"default_portal": "psugo" if has_payg else "parent",
	}
