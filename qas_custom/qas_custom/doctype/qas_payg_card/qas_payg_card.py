import frappe
from frappe.model.document import Document
from qas_custom.qas_custom.doctype.payg_validation import nonnegative, integer, require_equal

class QASPAYGCard(Document):
    def validate(self):
        nonnegative(self, "unit_price_snapshot")
        balances = tuple(integer(self, field) for field in
                         ("available_count", "reserved_count", "consumed_count"))
        if self.issued_on and self.expires_on and str(self.expires_on) < str(self.issued_on):
            frappe.throw("Card expiry cannot precede issue date")
        require_equal(self, "customer", frappe.db.get_value("Parent", self.family_parent, "customer"))
        require_equal(self, "course", frappe.db.get_value("QAS PAYG Product", self.product, "course"))
        if self.is_new() and any(balances):
            frappe.throw("New PAYG cards must start with empty balances")
        if self.is_new() and self.status != "Active":
            frappe.throw("New PAYG card must start Active")
        if not self.is_new():
            before = self.get_doc_before_save()
            if not before:
                frappe.throw("Existing PAYG card is required")
            for field in ("family_parent", "customer", "product", "course",
                          "issued_on", "unit_price_snapshot"):
                if str(self.get(field) or "") != str(before.get(field) or ""):
                    frappe.throw(f"PAYG card {field} cannot change")
            transitions = {
                "Active": {"Active", "Paused", "Transferred"},
                "Paused": {"Paused", "Active", "Transferred"},
                "Transferred": {"Transferred"},
            }
            if self.status not in transitions.get(before.status, set()):
                frappe.throw("PAYG card status transition is invalid")
            totals = frappe.db.sql("""select coalesce(sum(available_delta),0), coalesce(sum(reserved_delta),0), coalesce(sum(consumed_delta),0) from `tabQAS PAYG Entry` where card=%s""", self.name)[0]
            if balances != tuple(int(x) for x in totals):
                frappe.throw("Card balances must equal its ledger entries")
