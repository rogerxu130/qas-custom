from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from qas_custom.modules.billing.invoice_settings import SETTINGS_DOCTYPE


LEDGER_DOCTYPE = "QAS Store Credit Ledger"
STORE_CREDIT_LIABILITY_ACCOUNT_NAME = "Store Credit Liability"
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
		fields = [
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
		]
		if frappe.db.has_column(LEDGER_DOCTYPE, "journal_entry"):
			fields.append("journal_entry")
		rows = frappe.get_all(
			LEDGER_DOCTYPE,
			filters={"customer": customer},
			fields=fields,
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
	journal_entry: str | None = None,
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
	if frappe.db.has_column(LEDGER_DOCTYPE, "journal_entry"):
		doc.journal_entry = journal_entry
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
	journal_entry = ensure_store_credit_journal_entry(invoice_doc, apply_amount, ledger=entry.name)
	if journal_entry and frappe.db.has_column(LEDGER_DOCTYPE, "journal_entry"):
		frappe.db.set_value(LEDGER_DOCTYPE, entry.name, "journal_entry", journal_entry, update_modified=False)
	sync_invoice_store_credit_snapshot(invoice_doc.name)
	return {
		"applied": apply_amount,
		"balance": entry.balance_after,
		"ledger": entry.name,
		"journal_entry": journal_entry,
		"skipped": False,
	}


def get_invoice_store_credit_applied(invoice: str) -> float:
	return _invoice_store_credit_applied(invoice)


def get_invoice_payable_amount(invoice_doc) -> float:
	invoice_name = _doc_field(invoice_doc, "name")
	total = flt(_doc_field(invoice_doc, "grand_total") or _doc_field(invoice_doc, "rounded_total") or 0)
	outstanding = _invoice_outstanding_amount(invoice_doc)
	docstatus = cint(_doc_field(invoice_doc, "docstatus") or 0)
	applied = get_invoice_store_credit_applied(invoice_name)
	if docstatus == 2:
		return 0
	if docstatus == 1:
		if has_invoice_store_credit_journal_entry(invoice_name) or outstanding <= 0:
			return max(0, outstanding)
		return max(0, outstanding - applied)
	return max(0, total - applied)


def sync_invoice_store_credit_snapshot(invoice_doc):
	if not invoice_doc:
		return None
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	updates = {}
	if frappe.db.has_column("Sales Invoice", "qas_store_credit_applied"):
		updates["qas_store_credit_applied"] = get_invoice_store_credit_applied(doc.name)
	if frappe.db.has_column("Sales Invoice", "qas_amount_payable"):
		updates["qas_amount_payable"] = get_invoice_payable_amount(doc)
	if updates:
		frappe.db.set_value("Sales Invoice", doc.name, updates, update_modified=False)
	return updates


def ensure_store_credit_journal_entry(invoice_doc, amount: float | None = None, ledger: str | None = None):
	if not _doctype_available("Journal Entry"):
		frappe.throw(_("Journal Entry is required to apply store credit to an invoice."))
	doc = frappe.get_doc("Sales Invoice", invoice_doc) if isinstance(invoice_doc, str) else invoice_doc
	amount = flt(amount if amount is not None else get_invoice_store_credit_applied(doc.name))
	if amount <= 0:
		return None

	existing = _existing_store_credit_journal_entry(invoice=doc.name, ledger=ledger)
	if existing:
		return existing

	company = doc.get("company")
	customer = doc.get("customer")
	receivable_account = doc.get("debit_to")
	if not company or not customer or not receivable_account:
		frappe.throw(_("Invoice {0} is missing company, customer, or receivable account.").format(doc.name))

	liability_account = get_store_credit_liability_account(company=company, create_if_missing=True)
	if not liability_account:
		frappe.throw(_("Store credit liability account is required."))

	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		journal = frappe.new_doc("Journal Entry")
		journal.voucher_type = "Journal Entry"
		journal.company = company
		journal.posting_date = doc.get("posting_date") or nowdate()
		journal.user_remark = _("Store credit applied to Sales Invoice {0}.").format(doc.name)
		_set_if_has_field(journal, "qas_store_credit_invoice", doc.name)
		_set_if_has_field(journal, "qas_store_credit_ledger", ledger)
		_set_if_has_field(journal, "qas_store_credit_amount", amount)
		journal.append(
			"accounts",
			{
				"account": liability_account,
				"debit_in_account_currency": amount,
			},
		)
		journal.append(
			"accounts",
			{
				"account": receivable_account,
				"party_type": "Customer",
				"party": customer,
				"credit_in_account_currency": amount,
				"reference_type": "Sales Invoice",
				"reference_name": doc.name,
			},
		)
		journal.flags.ignore_permissions = True
		journal.insert(ignore_permissions=True)
		journal.submit()
	finally:
		frappe.set_user(original_user)
	return journal.name


def cancel_store_credit_journal_entries(invoice: str):
	if not invoice or not _doctype_available("Journal Entry"):
		return []
	names = _store_credit_journal_entries(invoice=invoice, docstatus=1)
	cancelled = []
	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		for name in names:
			journal = frappe.get_doc("Journal Entry", name)
			if cint(journal.docstatus) != 1:
				continue
			journal.flags.ignore_permissions = True
			journal.cancel()
			cancelled.append(name)
	finally:
		frappe.set_user(original_user)
	return cancelled


def has_invoice_store_credit_journal_entry(invoice: str) -> bool:
	return bool(_store_credit_journal_entries(invoice=invoice, docstatus=1))


def get_store_credit_liability_account(company: str | None = None, *, create_if_missing: bool = False):
	if not company:
		return None
	configured = _configured_store_credit_liability_account(company)
	if configured:
		return configured
	existing = frappe.db.get_value(
		"Account",
		{"account_name": STORE_CREDIT_LIABILITY_ACCOUNT_NAME, "company": company, "is_group": 0},
		"name",
	)
	if existing:
		_sync_settings_liability_account(existing)
		return existing
	if not create_if_missing:
		return None
	parent = _store_credit_liability_parent_account(company)
	if not parent:
		frappe.throw(_("Could not find a liability group account for company {0}.").format(company))
	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": STORE_CREDIT_LIABILITY_ACCOUNT_NAME,
			"company": company,
			"parent_account": parent,
			"is_group": 0,
			"root_type": "Liability",
			"report_type": "Balance Sheet",
		}
	)
	account.insert(ignore_permissions=True)
	_sync_settings_liability_account(account.name)
	return account.name


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


def _invoice_outstanding_amount(invoice_doc) -> float:
	invoice_name = _doc_field(invoice_doc, "name")
	if invoice_name and frappe.db.has_column("Sales Invoice", "outstanding_amount"):
		value = frappe.db.get_value("Sales Invoice", invoice_name, "outstanding_amount")
		if value is not None:
			return flt(value)
	return flt(_doc_field(invoice_doc, "outstanding_amount") or 0)


def _doc_field(doc, fieldname: str):
	if hasattr(doc, "get"):
		return doc.get(fieldname)
	return getattr(doc, fieldname, None)


def _existing_store_credit_journal_entry(invoice: str, ledger: str | None = None):
	if ledger and frappe.db.has_column("Journal Entry", "qas_store_credit_ledger"):
		name = frappe.db.get_value("Journal Entry", {"qas_store_credit_ledger": ledger, "docstatus": ["!=", 2]}, "name")
		if name:
			return name
	names = _store_credit_journal_entries(invoice=invoice, docstatus=1)
	return names[0] if names and not ledger else None


def _store_credit_journal_entries(invoice: str, docstatus: int | None = None):
	if not invoice or not _doctype_available("Journal Entry"):
		return []
	if frappe.db.has_column("Journal Entry", "qas_store_credit_invoice"):
		filters = {"qas_store_credit_invoice": invoice}
		if docstatus is not None:
			filters["docstatus"] = docstatus
		else:
			filters["docstatus"] = ["!=", 2]
		return frappe.get_all("Journal Entry", filters=filters, pluck="name", order_by="creation asc")
	if not _doctype_available("Journal Entry Account"):
		return []
	rows = frappe.get_all(
		"Journal Entry Account",
		filters={
			"reference_type": "Sales Invoice",
			"reference_name": invoice,
			"parenttype": "Journal Entry",
		},
		fields=["parent"],
		limit_page_length=0,
	)
	names = sorted({row.get("parent") for row in rows if row.get("parent")})
	if not names:
		return []
	filters = {"name": ["in", names]}
	if docstatus is not None:
		filters["docstatus"] = docstatus
	else:
		filters["docstatus"] = ["!=", 2]
	return frappe.get_all("Journal Entry", filters=filters, pluck="name", order_by="creation asc")


def _configured_store_credit_liability_account(company: str):
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE) or not frappe.db.has_column(SETTINGS_DOCTYPE, "store_credit_liability_account"):
		return None
	account = frappe.db.get_single_value(SETTINGS_DOCTYPE, "store_credit_liability_account")
	if not account:
		return None
	if frappe.db.get_value("Account", account, "company") != company:
		return None
	return account


def _sync_settings_liability_account(account: str):
	if not account or not frappe.db.exists("DocType", SETTINGS_DOCTYPE) or not frappe.db.has_column(SETTINGS_DOCTYPE, "store_credit_liability_account"):
		return
	current = frappe.db.get_single_value(SETTINGS_DOCTYPE, "store_credit_liability_account")
	if not current:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "store_credit_liability_account", account)


def _store_credit_liability_parent_account(company: str):
	for filters in (
		{"company": company, "root_type": "Liability", "is_group": 1, "account_name": "Current Liabilities"},
		{"company": company, "root_type": "Liability", "is_group": 1, "account_type": "Payable"},
		{"company": company, "root_type": "Liability", "is_group": 1},
	):
		account = frappe.db.get_value("Account", filters, "name", order_by="lft desc")
		if account:
			return account
	return None


def _set_if_has_field(doc, fieldname: str, value):
	if value is not None and doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _doctype_available(doctype: str) -> bool:
	return frappe.db.exists("DocType", doctype) and frappe.db.table_exists(doctype)


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
