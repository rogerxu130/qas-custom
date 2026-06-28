from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate


LEDGER_DOCTYPE = "QAS Store Credit Ledger"
COURSE_INVOICE_TYPES = {"Course", "Store Credit Top-up", "Holiday Program"}
COURSE_LINE_TYPES = {"Course Fee", "Trial Fee", "Makeup", "Pay-as-you-go", "Holiday Program"}
BALANCE_FIELDS = ("store_credit", "credit_balance", "available_credit", "balance")


def get_store_credit_balance(parent: str | None = None, customer: str | None = None) -> float:
	if not _ledger_available():
		return _legacy_balance(parent=parent, customer=customer)

	filters = {}
	if customer:
		filters["customer"] = customer
	elif parent:
		filters["parent"] = parent
	else:
		return 0

	credit = frappe.db.get_value(LEDGER_DOCTYPE, filters, "sum(credit_amount)") or 0
	debit = frappe.db.get_value(LEDGER_DOCTYPE, filters, "sum(debit_amount)") or 0
	return flt(credit) - flt(debit)


def get_store_credit_summary(parent: str | None = None, customer: str | None = None, limit: int = 50):
	parent, customer = resolve_parent_customer(parent=parent, customer=customer)
	balance = get_store_credit_balance(parent=parent, customer=customer)
	rows = []
	if _ledger_available() and customer:
		rows = frappe.get_all(
			LEDGER_DOCTYPE,
			filters={"customer": customer},
			fields=[
				"name",
				"posting_date",
				"transaction_type",
				"credit_amount",
				"debit_amount",
				"payment_amount",
				"balance_after",
				"invoice",
				"reason",
				"notes",
				"creation",
			],
			order_by="creation desc",
			limit=limit,
		)
	return {"parent": parent, "customer": customer, "balance": balance, "items": rows}


def create_store_credit_entry(
	*,
	customer: str,
	parent: str | None = None,
	student: str | None = None,
	transaction_type: str,
	credit_amount: float = 0,
	debit_amount: float = 0,
	payment_amount: float = 0,
	invoice: str | None = None,
	payment_entry: str | None = None,
	enrollment: str | None = None,
	reference_doctype: str | None = None,
	reference_document: str | None = None,
	source_doctype: str | None = None,
	source_document: str | None = None,
	reason: str | None = None,
	notes: str | None = None,
	posting_date: str | None = None,
):
	if not _ledger_available():
		frappe.throw(_("QAS Store Credit Ledger is not installed. Run migration first."))
	if not customer:
		frappe.throw(_("Customer is required for store credit."))

	credit_amount = flt(credit_amount)
	debit_amount = flt(debit_amount)
	payment_amount = flt(payment_amount)
	if credit_amount <= 0 and debit_amount <= 0:
		frappe.throw(_("Store credit entry must have a credit or debit amount."))
	if credit_amount > 0 and debit_amount > 0:
		frappe.throw(_("Store credit entry cannot be both credit and debit."))

	parent, customer = resolve_parent_customer(parent=parent, customer=customer)
	current_balance = get_store_credit_balance(parent=parent, customer=customer)
	next_balance = current_balance + credit_amount - debit_amount
	if next_balance < -0.0001:
		frappe.throw(_("Insufficient store credit balance."))

	doc = frappe.new_doc(LEDGER_DOCTYPE)
	doc.posting_date = posting_date or nowdate()
	doc.parent = parent
	doc.customer = customer
	doc.student = student
	doc.transaction_type = transaction_type
	doc.credit_amount = credit_amount
	doc.debit_amount = debit_amount
	doc.payment_amount = payment_amount
	doc.balance_after = next_balance
	doc.invoice = invoice
	doc.payment_entry = payment_entry
	doc.enrollment = enrollment
	doc.reference_doctype = reference_doctype
	doc.reference_document = reference_document
	doc.source_doctype = source_doctype
	doc.source_document = source_document
	doc.reason = reason
	doc.notes = notes
	doc.insert(ignore_permissions=True)

	sync_cached_balance(parent=parent, customer=customer, balance=next_balance)
	return doc


def adjust_store_credit(parent: str | None = None, customer: str | None = None, amount: float = 0, reason: str | None = None, notes: str | None = None):
	parent, customer = resolve_parent_customer(parent=parent, customer=customer)
	amount = flt(amount)
	if amount == 0:
		frappe.throw(_("Adjustment amount is required."))
	return create_store_credit_entry(
		parent=parent,
		customer=customer,
		transaction_type="Manual Adjustment",
		credit_amount=amount if amount > 0 else 0,
		debit_amount=abs(amount) if amount < 0 else 0,
		reason=reason or "School Admin adjustment",
		notes=notes,
		source_doctype="User",
		source_document=frappe.session.user,
	)


def apply_store_credit_to_invoice(invoice_doc):
	if not _is_course_invoice(invoice_doc):
		return {"applied": 0, "balance": 0, "skipped": True, "reason": "Invoice is not course-related."}

	parent, customer = resolve_parent_customer(parent=invoice_doc.get("parent"), customer=invoice_doc.customer)
	available = get_store_credit_balance(parent=parent, customer=customer)
	amount_due = flt(invoice_doc.grand_total or invoice_doc.rounded_total or 0)
	already_applied = _invoice_store_credit_applied(invoice_doc.name)
	remaining = max(0, amount_due - already_applied)
	apply_amount = min(available, remaining)
	if apply_amount <= 0:
		return {"applied": 0, "balance": available, "skipped": True, "reason": "No store credit available."}

	entry = create_store_credit_entry(
		parent=parent,
		customer=customer,
		student=invoice_doc.get("primary_student") or invoice_doc.get("student"),
		transaction_type="Invoice Application",
		debit_amount=apply_amount,
		invoice=invoice_doc.name,
		enrollment=invoice_doc.get("enrollment"),
		reference_doctype="Sales Invoice",
		reference_document=invoice_doc.name,
		source_doctype="Sales Invoice",
		source_document=invoice_doc.name,
		reason="Applied on invoice approval",
		notes=_("Applied store credit to invoice {0}.").format(invoice_doc.name),
	)
	return {"applied": apply_amount, "balance": entry.balance_after, "ledger": entry.name, "skipped": False}


def get_invoice_store_credit_applied(invoice: str) -> float:
	return _invoice_store_credit_applied(invoice)


def get_invoice_payable_amount(invoice_doc) -> float:
	total = flt(invoice_doc.get("grand_total") or invoice_doc.get("rounded_total") or 0)
	outstanding = flt(invoice_doc.get("outstanding_amount") or 0)
	base_amount = outstanding if outstanding > 0 else total
	return max(0, base_amount - get_invoice_store_credit_applied(invoice_doc.name))


def resolve_parent_customer(parent: str | None = None, customer: str | None = None):
	if parent and not customer and frappe.db.has_column("Parent", "customer"):
		customer = frappe.db.get_value("Parent", parent, "customer")
	if customer and not parent and frappe.db.has_column("Parent", "customer"):
		parent = frappe.db.get_value("Parent", {"customer": customer}, "name")
	if not customer:
		frappe.throw(_("Customer is required for store credit."))
	return parent, customer


def sync_cached_balance(parent: str | None, customer: str | None, balance: float | None = None):
	if balance is None:
		balance = get_store_credit_balance(parent=parent, customer=customer)
	if parent:
		_sync_balance_fields("Parent", parent, balance)
	if customer:
		_sync_balance_fields("Customer", customer, balance)
	return balance


def _invoice_store_credit_applied(invoice: str) -> float:
	if not _ledger_available() or not invoice:
		return 0
	return flt(
		frappe.db.get_value(
			LEDGER_DOCTYPE,
			{"invoice": invoice, "transaction_type": "Invoice Application"},
			"sum(debit_amount)",
		)
		or 0
	)


def _is_course_invoice(invoice_doc) -> bool:
	invoice_type = invoice_doc.get("qas_invoice_type") if hasattr(invoice_doc, "get") else None
	if invoice_type in {"Material Order", "Other"}:
		return False
	if invoice_type in COURSE_INVOICE_TYPES:
		return True
	for item in invoice_doc.get("items", []):
		line_type = item.get("qas_line_type") if hasattr(item, "get") else None
		if line_type in COURSE_LINE_TYPES:
			return True
	return False


def _legacy_balance(parent: str | None = None, customer: str | None = None):
	for doctype, name in (("Parent", parent), ("Customer", customer)):
		if not name:
			continue
		for fieldname in BALANCE_FIELDS:
			if frappe.db.has_column(doctype, fieldname):
				value = frappe.db.get_value(doctype, name, fieldname)
				if value is not None:
					return flt(value)
	return 0


def _sync_balance_fields(doctype: str, name: str, balance: float):
	if not name:
		return
	for fieldname in BALANCE_FIELDS:
		if frappe.db.has_column(doctype, fieldname):
			frappe.db.set_value(doctype, name, fieldname, flt(balance), update_modified=False)
			break


def _ledger_available():
	return frappe.db.table_exists(LEDGER_DOCTYPE)
