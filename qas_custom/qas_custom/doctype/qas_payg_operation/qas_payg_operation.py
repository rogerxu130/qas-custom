import frappe
from frappe.model.document import Document
from qas_custom.modules.payg.money import stored_currency
from qas_custom.modules.payg.rules import as_brisbane_datetime
from qas_custom.modules.payg.invoice_links import permitted_invoice_relink
from qas_custom.qas_custom.doctype.payg_validation import nonnegative, integer


def _audit_value(field, value):
    if field in ("old_price", "new_price", "price_delta"):
        return None if value is None or value == "" else stored_currency(value)
    if field == "created_at":
        return None if value is None or value == "" else as_brisbane_datetime(value).replace(tzinfo=None)
    return str(value or "")


class QASPAYGOperation(Document):
    def validate(self):
        if self.is_new():
            if self.status != "Pending":
                frappe.throw("New PAYG operation must start Pending")
        else:
            self._validate_existing()
        for field in ("old_price", "new_price"):
            nonnegative(self, field)
        quantity = integer(self, "quantity")
        if not self.family_parent:
            frappe.throw("Operation family is required")
        family_customer = frappe.db.get_value("Parent", self.family_parent, "customer")
        if self.customer and self.customer != family_customer:
            frappe.throw("Operation customer does not match family")
        cards = {}
        for field in ("source_card", "target_card", "card"):
            if self.get(field):
                linked = frappe.db.get_value(
                    "QAS PAYG Card", self.get(field),
                    ("family_parent", "customer", "product", "course"), as_dict=True)
                if not linked or linked.family_parent != self.family_parent or linked.customer != family_customer:
                    frappe.throw(f"{field} does not match operation family/customer")
                cards[field] = linked

        if self.operation_type == "Purchase":
            self._validate_purchase(cards, family_customer)
        elif self.operation_type == "Exchange":
            self._validate_exchange(cards, family_customer, quantity)
        elif self.operation_type == "ExpiryChange":
            if not self.card or not self.old_expiry or not self.new_expiry:
                frappe.throw("Expiry change requires card and old/new expiry")
        else:
            frappe.throw("Unsupported PAYG operation type")

    def _validate_existing(self):
        before = self.get_doc_before_save()
        if not before:
            frappe.throw("Existing PAYG operation is required")
        for field in ("operation_type", "request_key", "family_parent", "customer",
                      "product", "source_card", "old_course", "new_course",
                      "old_expiry", "new_expiry", "old_price", "new_price",
                      "quantity", "price_delta", "actor", "created_at", "reason"):
            if _audit_value(field, self.get(field)) != _audit_value(field, before.get(field)):
                frappe.throw(f"PAYG operation {field} cannot change")
        for field in ("card", "target_card", "invoice", "issue_request_key", "invoice_request_key"):
            old_value, new_value = before.get(field), self.get(field)
            if old_value and new_value != old_value:
                if field == "invoice" and permitted_invoice_relink(self.name, old_value, new_value):
                    continue
                frappe.throw(f"PAYG operation {field} can only be set once")
        transitions = {"Pending": {"Pending", "Completed", "Cancelled"},
                       "Completed": {"Completed"}, "Cancelled": {"Cancelled"}}
        if self.status not in transitions.get(before.status, set()):
            frappe.throw("PAYG operation status transition is invalid")

    def _validate_purchase(self, cards, family_customer):
        if not self.customer or not family_customer or not self.product:
            frappe.throw("Purchase requires family customer and product")
        if self.source_card:
            frappe.throw("Purchase cannot have a source card")
        if self.new_price is None or stored_currency(self.new_price) <= 0:
            frappe.throw("Purchase requires a positive unit price snapshot")
        product_course = frappe.db.get_value("QAS PAYG Product", self.product, "course")
        if not product_course or not self.new_course or self.new_course != product_course:
            frappe.throw("Purchase product/course snapshot is required and must match")
        for field in ("card", "target_card"):
            linked = cards.get(field)
            if linked and (linked.product != self.product or linked.course != product_course):
                frappe.throw(f"Purchase {field} must match product and course")

    def _validate_exchange(self, cards, family_customer, quantity):
        if not self.customer or not family_customer or not self.source_card or not self.product:
            frappe.throw("Exchange requires customer, source card, and target product")
        if not self.old_course or not self.new_course or self.old_price is None or self.new_price is None:
            frappe.throw("Exchange requires old/new course and price")
        if quantity <= 0:
            frappe.throw("Exchange quantity must be positive")
        source = cards["source_card"]
        if source.course != self.old_course:
            frappe.throw("Exchange source course does not match snapshot")
        if frappe.db.get_value("QAS PAYG Product", self.product, "course") != self.new_course:
            frappe.throw("Exchange target product and course differ")
        if self.status == "Completed" and not self.target_card:
            frappe.throw("Completed exchange requires target card")
        if self.target_card:
            target = cards["target_card"]
            if self.source_card == self.target_card or target.product != self.product or target.course != self.new_course:
                frappe.throw("Exchange target card must be distinct and match product/course")
        if self.card and self.card != self.source_card:
            frappe.throw("Exchange card must be its source card")
