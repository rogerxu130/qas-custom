from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from qas_custom.modules.billing.invoice_settings import SETTINGS_DOCTYPE, get_invoice_settings, settings_doctype_available
from qas_custom.utils.environment import payment_block_reason, payment_mutations_enabled


LEDGER_DOCTYPE = "QAS Store Credit Ledger"
STORE_CREDIT_LIABILITY_ACCOUNT_NAME = "Store Credit Liability"
COURSE_INVOICE_TYPES = {"Course", "Store Credit Top-up", "Holiday Program"}
COURSE_LINE_TYPES = {"Course Fee", "Trial Fee", "Makeup", "Pay-as-you-go", "Holiday Program"}
BALANCE_FIELDS = ("store_credit", "credit_balance", "available_credit", "balance")
STORE_CREDIT_BONUS_TYPE = "Promotion Bonus"
STORE_CREDIT_BONUS_SCOPES = {"Both", "Top-up", "Invoice Payment"}


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


def get_store_credit_bonus_rules(scope: str | None = None):
	settings = get_invoice_settings()
	if not cint(settings.get("store_credit_bonus_enabled")):
		return []
	rules = settings.get("store_credit_bonus_rules") or []
	if not isinstance(rules, list):
		return []
	scope = _normalize_bonus_scope(scope) if scope else None
	matches = []
	for rule in rules:
		if not isinstance(rule, dict) or not cint(rule.get("enabled", 1)):
			continue
		applies_to = _normalize_bonus_scope(rule.get("applies_to"))
		if scope and applies_to not in {"Both", scope}:
			continue
		threshold = flt(rule.get("threshold_amount"))
		bonus = flt(rule.get("bonus_amount"))
		if threshold <= 0 or bonus <= 0:
			continue
		matches.append(
			{
				"enabled": 1,
				"threshold_amount": threshold,
				"bonus_amount": bonus,
				"applies_to": applies_to,
				"label": rule.get("label") or "",
			}
		)
	return sorted(matches, key=lambda row: row["threshold_amount"], reverse=True)


def get_store_credit_bonus_rule_for_amount(amount: float, scope: str | None = None):
	amount = flt(amount)
	if amount <= 0:
		return None
	for rule in get_store_credit_bonus_rules(scope=scope):
		if amount + 0.0001 >= flt(rule.get("threshold_amount")):
			return rule
	return None


def get_store_credit_bonus_for_source(source_doctype: str | None, source_document: str | None):
	if not _ledger_available() or not source_doctype or not source_document:
		return None
	name = frappe.db.get_value(
		LEDGER_DOCTYPE,
		{
			"transaction_type": STORE_CREDIT_BONUS_TYPE,
			"source_doctype": source_doctype,
			"source_document": source_document,
		},
		"name",
	)
	return frappe.get_doc(LEDGER_DOCTYPE, name).as_dict() if name else None


def grant_store_credit_bonus_for_amount(
	*,
	parent: str | None = None,
	customer: str | None = None,
	amount: float = 0,
	scope: str | None = None,
	source_doctype: str | None = None,
	source_document: str | None = None,
	invoice: str | None = None,
	payment_entry: str | None = None,
	student: str | None = None,
	enrollment: str | None = None,
	posting_date: str | None = None,
):
	amount = flt(amount)
	scope = _normalize_bonus_scope(scope)
	summary = {
		"created": False,
		"already_exists": False,
		"skipped": True,
		"reason": None,
		"payment_amount": amount,
		"bonus_amount": 0,
		"rule": None,
		"entry": None,
	}
	if amount <= 0:
		summary["reason"] = "Payment amount is required."
		return summary
	rule = get_store_credit_bonus_rule_for_amount(amount, scope=scope)
	if not rule:
		summary["reason"] = "No bonus rule matched."
		return summary
	if not source_doctype or not source_document:
		summary["reason"] = "Source document is required for duplicate protection."
		return summary

	existing = get_store_credit_bonus_for_source(source_doctype, source_document)
	if existing:
		summary.update(
			{
				"already_exists": True,
				"reason": "Bonus already granted for this source.",
				"entry": existing,
				"bonus_amount": flt(existing.get("credit_amount")),
				"rule": rule,
			}
		)
		return summary

	parent, customer = resolve_parent_customer(parent=parent, customer=customer)

	bonus_amount = flt(rule.get("bonus_amount"))
	entry = create_store_credit_entry(
		parent=parent,
		customer=customer,
		student=student,
		transaction_type=STORE_CREDIT_BONUS_TYPE,
		credit_amount=bonus_amount,
		payment_amount=amount,
		invoice=invoice,
		payment_entry=payment_entry,
		enrollment=enrollment,
		reference_doctype=source_doctype,
		reference_document=source_document,
		source_doctype=source_doctype,
		source_document=source_document,
		reason=rule.get("label") or "Store credit promotion bonus",
		notes=_("Automatic store credit bonus for a single {0} amount of {1}.").format(scope.lower(), amount),
		posting_date=posting_date,
	)
	summary.update(
		{
			"created": True,
			"skipped": False,
			"reason": None,
			"bonus_amount": bonus_amount,
			"rule": rule,
			"entry": entry.as_dict(),
		}
	)
	return summary


def grant_store_credit_bonus_for_payment_entry(payment_entry):
	if not payment_entry:
		return None
	doc = frappe.get_doc("Payment Entry", payment_entry) if isinstance(payment_entry, str) else payment_entry
	if doc.get("payment_type") and doc.get("payment_type") != "Receive":
		return {"created": False, "skipped": True, "reason": "Payment is not a received payment."}

	amount = flt(doc.get("paid_amount") or doc.get("received_amount") or 0)
	if amount <= 0:
		return {"created": False, "skipped": True, "reason": "Payment amount is required."}

	invoice_name = None
	for row in doc.get("references", []):
		if row.get("reference_doctype") == "Sales Invoice" and row.get("reference_name"):
			invoice_name = row.get("reference_name")
			break
	if not invoice_name:
		return {"created": False, "skipped": True, "reason": "No linked sales invoice."}

	invoice_doc = frappe.get_doc("Sales Invoice", invoice_name) if invoice_name and _doctype_available("Sales Invoice") else None
	customer = doc.get("party") if doc.get("party_type") == "Customer" else None
	if not customer and invoice_doc:
		customer = invoice_doc.get("customer")
	parent = invoice_doc.get("parent") if invoice_doc else None
	student = (invoice_doc.get("primary_student") or invoice_doc.get("student")) if invoice_doc else None
	enrollment = invoice_doc.get("enrollment") if invoice_doc else None

	if not customer:
		return {"created": False, "skipped": True, "reason": "Customer could not be resolved."}

	return grant_store_credit_bonus_for_amount(
		parent=parent,
		customer=customer,
		amount=amount,
		scope="Invoice Payment",
		source_doctype="Payment Entry",
		source_document=doc.name,
		invoice=invoice_name,
		payment_entry=doc.name,
		student=student,
		enrollment=enrollment,
		posting_date=doc.get("posting_date"),
	)


def grant_store_credit_bonus_on_payment_entry_submit(doc, method=None):
	try:
		result = grant_store_credit_bonus_for_payment_entry(doc)
		if result and result.get("created"):
			doc.add_comment("Comment", _("Store credit bonus granted: {0}.").format(flt(result.get("bonus_amount"))))
	except Exception:
		frappe.log_error(frappe.get_traceback(), "QAS Store Credit Bonus Failed")


def apply_store_credit_to_unpaid_invoices(parent: str | None = None, customer: str | None = None, limit: int = 100):
	parent, customer = resolve_parent_customer(parent=parent, customer=customer)
	balance = get_store_credit_balance(parent=parent, customer=customer)
	summary = {
		"applied": 0,
		"balance": balance,
		"invoices": [],
		"skipped": False,
		"reason": None,
		"skipped_non_course_invoices": 0,
	}
	if balance <= 0:
		summary.update({"skipped": True, "reason": "No store credit available."})
		return summary
	if not _doctype_available("Sales Invoice"):
		summary.update({"skipped": True, "reason": "Sales Invoice is not installed."})
		return summary

	filters = {"customer": customer, "docstatus": 1}
	if frappe.db.has_column("Sales Invoice", "outstanding_amount"):
		filters["outstanding_amount"] = [">", 0.005]

	invoice_names = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		pluck="name",
		order_by="due_date asc, creation asc",
		limit=max(1, cint(limit) or 100),
	)
	if not invoice_names:
		summary.update({"skipped": True, "reason": "No submitted unpaid invoices found."})
		return summary

	for invoice_name in invoice_names:
		if get_store_credit_balance(parent=parent, customer=customer) <= 0:
			break
		invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if cint(invoice_doc.get("docstatus") or 0) != 1 or _invoice_outstanding_amount(invoice_doc) <= 0.005:
			continue
		if not _is_course_invoice(invoice_doc):
			summary["skipped_non_course_invoices"] += 1
			continue
		application = apply_store_credit_to_invoice(
			invoice_doc,
			reason="Applied after manual store credit adjustment",
			notes=_("Automatically applied store credit to existing unpaid invoice {0}.").format(invoice_doc.name),
		)
		applied = flt(application.get("applied"))
		if applied <= 0:
			continue
		invoice_doc.add_comment(
			"Comment",
			_("Store credit automatically applied after manual adjustment: {0}.").format(applied),
		)
		summary["applied"] = flt(summary["applied"]) + applied
		summary["invoices"].append(
			{
				"invoice": invoice_doc.name,
				"applied": applied,
				"balance": application.get("balance"),
				"ledger": application.get("ledger"),
				"journal_entry": application.get("journal_entry"),
			}
		)

	summary["balance"] = get_store_credit_balance(parent=parent, customer=customer)
	if not summary["invoices"]:
		summary.update({"skipped": True, "reason": "No eligible course invoices needed store credit."})
	return summary


def apply_store_credit_to_invoice(invoice_doc, *, reason: str | None = None, notes: str | None = None):
	if not _is_course_invoice(invoice_doc):
		return {"applied": 0, "balance": 0, "skipped": True, "reason": "Invoice is not course-related."}

	parent, customer = resolve_parent_customer(parent=invoice_doc.get("parent"), customer=invoice_doc.customer)
	available = get_store_credit_balance(parent=parent, customer=customer)
	already_applied = _invoice_store_credit_applied(invoice_doc.name)
	remaining = _store_credit_remaining_amount(invoice_doc, already_applied=already_applied)
	if remaining <= 0:
		sync_invoice_store_credit_snapshot(invoice_doc.name)
		return {
			"applied": 0,
			"already_applied": already_applied,
			"balance": available,
			"skipped": True,
			"reason": "Store credit already applied.",
		}
	apply_amount = min(available, remaining)
	if apply_amount <= 0:
		sync_invoice_store_credit_snapshot(invoice_doc.name)
		return {
			"applied": 0,
			"already_applied": already_applied,
			"balance": available,
			"skipped": True,
			"reason": "No store credit available.",
		}

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
		reason=reason or "Applied on invoice approval",
		notes=notes or _("Applied store credit to invoice {0}.").format(invoice_doc.name),
	)
	journal_entry = ensure_store_credit_journal_entry(invoice_doc, apply_amount, ledger=entry.name)
	if journal_entry and frappe.db.has_column(LEDGER_DOCTYPE, "journal_entry"):
		frappe.db.set_value(LEDGER_DOCTYPE, entry.name, "journal_entry", journal_entry, update_modified=False)
	sync_invoice_store_credit_snapshot(invoice_doc.name)
	return {
		"applied": apply_amount,
		"already_applied": already_applied,
		"balance": entry.balance_after,
		"ledger": entry.name,
		"journal_entry": journal_entry,
		"skipped": False,
	}


def apply_store_credit_on_sales_invoice_submit(doc, method=None):
	if not doc or doc.doctype != "Sales Invoice":
		return
	application = apply_store_credit_to_invoice(doc)
	if flt(application.get("applied")) > 0:
		doc.add_comment("Comment", _("Store credit applied: {0}.").format(flt(application.get("applied"))))


def get_invoice_store_credit_applied(invoice: str) -> float:
	return _invoice_store_credit_applied(invoice)


def _store_credit_remaining_amount(invoice_doc, *, already_applied: float = 0) -> float:
	total = flt(_doc_field(invoice_doc, "rounded_total") or _doc_field(invoice_doc, "grand_total") or 0)
	total_remaining = max(0, total - flt(already_applied))
	docstatus = cint(_doc_field(invoice_doc, "docstatus") or 0)
	if docstatus == 1:
		outstanding = max(0, _invoice_outstanding_amount(invoice_doc))
		return min(outstanding, total_remaining) if flt(already_applied) > 0 else outstanding
	return total_remaining


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
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))
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

	original_user = frappe.session.user or "Administrator"
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
	if not payment_mutations_enabled():
		frappe.throw(_(payment_block_reason()))
	names = _store_credit_journal_entries(invoice=invoice, docstatus=1)
	cancelled = []
	original_user = frappe.session.user or "Administrator"
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
		return _invoice_store_credit_journal_amount(invoice)
	ledger_amount = flt(
		frappe.db.get_value(
			LEDGER_DOCTYPE,
			{"invoice": invoice, "transaction_type": "Invoice Application"},
			"sum(debit_amount)",
		)
		or 0
	)
	return ledger_amount or _invoice_store_credit_journal_amount(invoice)


def _invoice_store_credit_journal_amount(invoice: str) -> float:
	if not invoice or not _doctype_available("Journal Entry"):
		return 0
	journal_entries = _store_credit_journal_entries(invoice=invoice, docstatus=1)
	if not journal_entries:
		return 0
	amount = 0
	if frappe.db.has_column("Journal Entry", "qas_store_credit_amount"):
		amount = flt(
			frappe.db.get_value(
				"Journal Entry",
				{"name": ["in", journal_entries]},
				"sum(qas_store_credit_amount)",
			)
			or 0
		)
	if amount > 0:
		return amount
	if not _doctype_available("Journal Entry Account"):
		return 0
	rows = frappe.get_all(
		"Journal Entry Account",
		filters={
			"parent": ["in", journal_entries],
			"parenttype": "Journal Entry",
			"reference_type": "Sales Invoice",
			"reference_name": invoice,
		},
		fields=["credit_in_account_currency", "credit"],
		limit_page_length=0,
	)
	return sum(flt(row.get("credit_in_account_currency") or row.get("credit") or 0) for row in rows)


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
	if not settings_doctype_available():
		return None
	account = get_invoice_settings().get("store_credit_liability_account")
	if not account:
		return None
	if frappe.db.get_value("Account", account, "company") != company:
		return None
	return account


def _sync_settings_liability_account(account: str):
	if not account or not settings_doctype_available():
		return
	current = get_invoice_settings().get("store_credit_liability_account")
	if not current:
		try:
			frappe.db.set_single_value(SETTINGS_DOCTYPE, "store_credit_liability_account", account)
		except (KeyError, ImportError, frappe.DoesNotExistError):
			return


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


def _normalize_bonus_scope(scope: str | None):
	scope = (scope or "Both").strip()
	return scope if scope in STORE_CREDIT_BONUS_SCOPES else "Both"


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
