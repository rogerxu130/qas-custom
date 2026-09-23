import frappe
from frappe.model.document import Document
from qas_custom.qas_custom.doctype.payg_validation import integer


class QASPAYGEntry(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("PAYG entries are append-only")
        booking_kind = self.kind in ("Reserve", "Consume", "Return")
        operation_kind = self.kind in ("Issue", "Transfer Out", "Transfer In")
        if booking_kind and (not self.booking or self.operation):
            frappe.throw("Reservation entry must reference only its original booking")
        if operation_kind and (not self.operation or self.booking):
            frappe.throw("Issue and transfer entries must reference only their operation")
        if self.kind == "Correction":
            if not self.reason or bool(self.booking) == bool(self.operation):
                frappe.throw("Correction requires a reason and exactly one booking or operation")

        # All entry writers use the same lock order: Booking, Operation, Card,
        # then this booking's ledger rows. The card lock serializes balance writes.
        booking = frappe.get_doc("QAS PAYG Booking", self.booking, for_update=True) if self.booking else None
        operation = frappe.get_doc("QAS PAYG Operation", self.operation, for_update=True) if self.operation else None
        card = frappe.get_doc("QAS PAYG Card", self.card, for_update=True)
        if booking and booking.card != self.card:
            frappe.throw("Entry must use the original booking card")
        if operation:
            if operation.family_parent != card.family_parent:
                frappe.throw("Entry operation must belong to the card family")
            if self.kind == "Issue" and (operation.operation_type != "Purchase" or operation.card != self.card):
                frappe.throw("Issue must reference its Purchase operation card")
            if self.kind == "Transfer Out" and (operation.operation_type != "Exchange" or operation.source_card != self.card):
                frappe.throw("Transfer Out must reference its Exchange source card")
            if self.kind == "Transfer In" and (operation.operation_type != "Exchange" or operation.target_card != self.card):
                frappe.throw("Transfer In must reference its Exchange target card")

        deltas = tuple(integer(self, f, allow_negative=True) for f in
                       ("available_delta", "reserved_delta", "consumed_delta"))
        if not any(deltas):
            frappe.throw("PAYG entry must change a balance")
        if self.kind == "Issue" and deltas != (10, 0, 0):
            frappe.throw("Issue must add ten available sessions")
        if self.kind == "Transfer Out" and (deltas[0] >= 0 or deltas[1:] != (0, 0)):
            frappe.throw("Transfer Out must remove available sessions")
        if self.kind == "Transfer In" and (deltas[0] <= 0 or deltas[1:] != (0, 0)):
            frappe.throw("Transfer In must add available sessions")
        if booking_kind:
            rows = frappe.db.sql(
                """select kind from `tabQAS PAYG Entry`
                   where booking=%s order by creation, name for update""",
                (self.booking,), as_dict=True,
            )
            kinds = [row.kind for row in rows]
            reserves, consumes, returns = (kinds.count(kind) for kind in ("Reserve", "Consume", "Return"))
            if self.kind == "Reserve":
                if reserves or consumes or returns or deltas != (-1, 1, 0):
                    frappe.throw("Booking may reserve only once")
            elif self.kind == "Consume":
                if reserves != 1 or consumes or returns or deltas != (0, -1, 1):
                    frappe.throw("Booking must have one unconsumed reservation")
            elif self.kind == "Return":
                expected = (1, 0, -1) if consumes else (1, -1, 0)
                if reserves != 1 or consumes > 1 or returns or deltas != expected:
                    frappe.throw("Booking must return its own reserved or consumed session once")
        balances = tuple(integer(card, field) for field in
                         ("available_count", "reserved_count", "consumed_count"))
        next_balances = tuple(balance + delta for balance, delta in zip(balances, deltas))
        if any(value < 0 for value in next_balances):
            frappe.throw("PAYG entry would make a card balance negative")
        self._next_balances = next_balances

    def after_insert(self):
        if "_next_balances" not in self.__dict__:
            frappe.throw("PAYG entry must be validated before insertion")
        frappe.db.set_value(
            "QAS PAYG Card", self.card,
            dict(zip(("available_count", "reserved_count", "consumed_count"), self._next_balances)),
            update_modified=False,
        )

    def on_trash(self):
        frappe.throw("PAYG entries are append-only")
