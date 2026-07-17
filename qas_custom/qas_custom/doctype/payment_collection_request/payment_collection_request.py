from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


FACT_FIELDS = (
	"request_type",
	"campus",
	"parent",
	"customer",
	"invoice",
	"collected_amount",
	"received_at",
	"payment_method",
	"reference_no",
	"campus_admin_note",
	"submitted_by",
	"submitted_at",
	"invoice_outstanding_snapshot",
	"idempotency_key",
)


class PaymentCollectionRequest(Document):
	def validate(self):
		if self.request_type not in {"Invoice Payment", "Store Credit Top-up"}:
			frappe.throw(_("Invalid payment collection request type."))
		if self.status not in {"Pending Review", "Processed", "Rejected"}:
			frappe.throw(_("Invalid payment collection request status."))
		if flt(self.collected_amount) <= 0:
			frappe.throw(_("Collected amount must be greater than zero."))
		if self.payment_method not in {"Cash", "EFTPOS", "Bank Transfer", "Other"}:
			frappe.throw(_("Invalid payment method."))
		if self.request_type == "Invoice Payment" and not self.invoice:
			frappe.throw(_("Invoice is required for an Invoice Payment request."))
		if self.request_type == "Store Credit Top-up" and self.invoice:
			frappe.throw(_("Store Credit Top-up requests cannot be linked to an Invoice."))
		if self.status == "Rejected" and not (self.resolution_note or "").strip():
			frappe.throw(_("A rejection reason is required."))

		previous = self.get_doc_before_save()
		if not previous:
			return
		for fieldname in FACT_FIELDS:
			if previous.get(fieldname) != self.get(fieldname):
				frappe.throw(_("Submitted collection details cannot be changed."))
		if previous.status != "Pending Review" and previous.status != self.status:
			frappe.throw(_("Resolved payment collection requests cannot be changed."))
