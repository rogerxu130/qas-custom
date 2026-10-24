"""Recognise a late trial referral and replace its invoice in one transaction."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from qas_custom.modules.billing.store_credit import get_invoice_store_credit_applied
from qas_custom.modules.trial_referrals import (
	REFERRAL_VERIFIED, _get_trial_inquiry, _set_referrer, _validated_referrer,
	referral_invoice_message, referral_status, trial_referral_discount_amount,
)
from qas_custom.services.trial_invoice import REPLACEMENT_TRIAL_INVOICE_LOCK_PREFIX, _link_invoice


def add_invoiced_trial_referral(inquiry, referring_parent):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	with frappe.cache.lock(f"{REPLACEMENT_TRIAL_INVOICE_LOCK_PREFIX}:{inquiry}", timeout=60, blocking_timeout=10):
		try:
			result = _replace_invoiced_trial(inquiry, referring_parent)
			frappe.db.commit()
			return result
		except Exception:
			# Full rollback also discards queued after-commit notifications.
			frappe.db.rollback()
			raise


def _replace_invoiced_trial(inquiry, referring_parent):
	from qas_custom.services import school_admin as admin
	from qas_custom.modules.notifications.trial_referral_notifications import queue_referral_trial_discount_notification

	frappe.db.sql("select name from `tabInquiry` where name=%s for update", inquiry)
	doc = _get_trial_inquiry(inquiry)
	if doc.get("status") not in {"Booked", "Rescheduled", "Completed"}:
		frappe.throw(_("Only a booked or completed trial can have its invoice replaced for a late referral."))
	if referral_status(doc) == REFERRAL_VERIFIED or doc.get("referral_reward_ledger"):
		frappe.throw(_("This Inquiry already has a verified referral. Refresh to see its current invoice."))
	parent, customer = _validated_referrer(doc, referring_parent)
	invoice_name = doc.get("trial_invoice")
	if not invoice_name:
		frappe.throw(_("This action requires an existing linked Trial Invoice."))

	# Coordinate with Stripe accounting writes as well as manual invoice changes.
	frappe.db.sql("select name from `tabSales Invoice` where name=%s for update", invoice_name)
	old = frappe.get_doc("Sales Invoice", invoice_name)
	_validate_invoice(doc, old)
	discount = trial_referral_discount_amount()
	if discount <= 0 or discount > flt(old.grand_total):
		frappe.throw(_("The referral discount must be positive and cannot exceed the original invoice total."))

	_set_referrer(doc, parent=parent, customer=customer, actor=frappe.session.user)
	doc.referral_source = "Referral"
	doc.save(ignore_permissions=True)
	admin.cancel_school_admin_invoice_data(
		invoice=old.name,
		reason=_("Late referral recognised on Inquiry {0}; replacing with a discounted invoice.").format(doc.name),
		send_notifications=False,
		commit=False,
	)

	# Copy the original financial lines and settings, retaining any earlier adjustments.
	new = frappe.copy_doc(old)
	new.docstatus = 0
	new.status = "Draft"
	new.amended_from = old.name
	new.source_doctype = "Inquiry"
	new.source_document = doc.name
	new.source_type = "Replacement Trial Inquiry"
	new.parent = doc.parent
	new.student = doc.student
	new.qas_invoice_message = referral_invoice_message(doc)
	new.qas_apply_store_credit_on_submit = old.get("qas_apply_store_credit_on_submit", 1)
	adjustments = [
		{"description": row.description, "amount": flt(row.tax_amount)}
		for row in admin._invoice_adjustment_rows(old)
	]
	adjustments.append({"description": _("Referral discount"), "amount": -discount})
	admin._apply_invoice_adjustments(new, adjustments)
	admin.disable_sales_invoice_auto_notifications()
	admin._run_school_admin_invoice_mutation(lambda: new.insert(ignore_permissions=True))
	if abs(flt(new.grand_total) - (flt(old.grand_total) - discount)) > 0.01:
		frappe.throw(_("The replacement total does not match the original invoice less the referral discount. No changes were saved."))
	_link_invoice(doc.name, new.name)
	result = admin.submit_school_admin_invoice_data(invoice=new.name, enqueue_notification=True, commit=False)
	message = _("Late referral linked to Parent {0}. Invoice {1} replaced by discounted Invoice {2}.").format(parent, old.name, new.name)
	for doctype, name in (("Inquiry", doc.name), ("Sales Invoice", old.name), ("Sales Invoice", new.name)):
		admin._add_comment(doctype, name, message)
	queue_referral_trial_discount_notification(doc.name)
	return {"old_invoice": old.name, "invoice": new.name, "discount": discount, "notification": result.get("notification")}


def _validate_invoice(inquiry, invoice):
	from qas_custom.services.school_admin import _invoice_payment_amount

	if cint(invoice.docstatus) != 1:
		frappe.throw(_("The linked Trial Invoice must be submitted and active."))
	if (invoice.get("source_doctype") != "Inquiry" or invoice.get("source_document") != inquiry.name
		or invoice.get("parent") != inquiry.get("parent")
		or invoice.get("student") != inquiry.get("student")):
		frappe.throw(_("The linked invoice does not belong to this Trial Inquiry and family."))
	if invoice.get("is_return") or invoice.get("source_type") not in {"Trial Inquiry", "Replacement Trial Inquiry"}:
		frappe.throw(_("Only a standard Trial Invoice can be replaced for a referral."))
	if (_invoice_payment_amount(invoice.name) > 0 or flt(invoice.get("paid_amount")) > 0
		or get_invoice_store_credit_applied(invoice.name) > 0
		or flt(invoice.outstanding_amount) < flt(invoice.grand_total) - 0.01):
		frappe.throw(_("This invoice has a payment or Store Credit applied. Review its payment before replacing it for a referral."))
	if frappe.db.exists("DocType", "QAS Stripe Payment") and frappe.db.exists(
		"QAS Stripe Payment", {"invoice": invoice.name, "status": ["!=", "Expired"]}
	):
		frappe.throw(_("This invoice has a Stripe checkout or payment requiring review. Reconcile or expire it before replacing the invoice."))
