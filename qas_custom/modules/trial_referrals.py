from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from qas_custom.modules.billing.commands import get_invoice_customer
from qas_custom.modules.billing.invoice_settings import get_invoice_settings
from qas_custom.modules.billing.store_credit import LEDGER_DOCTYPE, create_store_credit_entry


REFERRAL_SOURCE_VALUES = {"referral", "referal", "referred"}
REFERRAL_PENDING = "Pending Verification"
REFERRAL_VERIFIED = "Verified"
REFERRAL_NOT_VERIFIED = "Not Verified"
REFERRAL_NOT_APPLICABLE = "Not Applicable"
REFERRAL_REWARD_TYPE = "Referral Reward"


def is_referral_claim(inquiry_doc) -> bool:
	"""Return whether the submitted inquiry selected the referral source."""
	return str(inquiry_doc.get("referral_source") or "").strip().casefold() in REFERRAL_SOURCE_VALUES


def referral_status(inquiry_doc) -> str:
	if not is_referral_claim(inquiry_doc):
		return REFERRAL_NOT_APPLICABLE
	return str(inquiry_doc.get("referral_status") or REFERRAL_PENDING).strip() or REFERRAL_PENDING


def referral_requires_review(inquiry_doc) -> bool:
	return is_referral_claim(inquiry_doc) and referral_status(inquiry_doc) == REFERRAL_PENDING


def prepare_referral_review(inquiry_doc, *, resume_status: str | None = None):
	"""Hold a submitted referral for manual identity verification before invoicing."""
	if not is_referral_claim(inquiry_doc):
		return False
	_set_if_field(inquiry_doc, "referral_status", REFERRAL_PENDING)
	if resume_status:
		_set_if_field(inquiry_doc, "referral_resume_status", resume_status)
	return True


def trial_referral_discount_amount() -> float:
	return max(0, flt(get_invoice_settings().get("referral_trial_discount") or 0))


def referral_conversion_reward_amount() -> float:
	return max(0, flt(get_invoice_settings().get("referral_conversion_reward") or 0))


def verified_referral_parent(inquiry_doc) -> str:
	if referral_status(inquiry_doc) != REFERRAL_VERIFIED:
		return ""
	return str(inquiry_doc.get("referring_parent") or "").strip()


def verified_trial_referral(inquiry_doc) -> bool:
	return bool(
		inquiry_doc.get("inquiry_type") == "Trial Lesson"
		and referral_status(inquiry_doc) == REFERRAL_VERIFIED
		and verified_referral_parent(inquiry_doc)
	)


def verify_trial_referral(inquiry: str, referring_parent: str, *, actor=None):
	"""Link the exact existing referrer and release the held trial invoice workflow."""
	doc = _get_trial_inquiry(inquiry)
	if not is_referral_claim(doc):
		frappe.throw(_("This Inquiry was not submitted as a referral."))
	if doc.get("status") == "Converted":
		frappe.throw(_("Use the historical referral action for an already converted Inquiry."))
	if referral_status(doc) != REFERRAL_PENDING:
		frappe.throw(_("This referral has already been reviewed."))

	parent, customer = _validated_referrer(doc, referring_parent)
	_set_referrer(doc, parent=parent, customer=customer, actor=actor)
	resume_status = _resume_status(doc)
	doc.status = resume_status
	_set_if_field(doc, "referral_resume_status", "")
	doc.save(ignore_permissions=True)
	_add_referral_comment(
		doc,
		_("Referral verified and linked to Parent {0}. Trial Invoice will include the referral discount.").format(parent),
	)
	return doc


def reject_trial_referral(inquiry: str, *, actor=None):
	"""Release an unverified referral at the standard trial price."""
	doc = _get_trial_inquiry(inquiry)
	if not is_referral_claim(doc):
		frappe.throw(_("This Inquiry was not submitted as a referral."))
	if doc.get("status") == "Converted":
		frappe.throw(_("An already converted Inquiry cannot be released for a Trial Invoice."))
	if referral_status(doc) != REFERRAL_PENDING:
		frappe.throw(_("This referral has already been reviewed."))

	_set_if_field(doc, "referral_status", REFERRAL_NOT_VERIFIED)
	_set_if_field(doc, "referring_parent", "")
	_set_if_field(doc, "referring_customer", "")
	_set_if_field(doc, "referral_verified_by", actor or frappe.session.user)
	_set_if_field(doc, "referral_verified_at", now_datetime())
	doc.status = _resume_status(doc)
	_set_if_field(doc, "referral_resume_status", "")
	doc.save(ignore_permissions=True)
	_add_referral_comment(
		doc,
		_("Referral could not be verified. Standard Trial Invoice workflow released."),
	)
	return doc


def recognise_converted_referral(inquiry: str, referring_parent: str, *, actor=None):
	"""Apply the referrer reward after a historical conversion without altering old billing."""
	doc = _get_trial_inquiry(inquiry)
	if doc.get("status") != "Converted":
		frappe.throw(_("Historical referral recognition is available only after an Inquiry is converted."))
	parent, customer = _validated_referrer(doc, referring_parent)
	if not is_referral_claim(doc):
		_set_if_field(doc, "referral_source", "Referral")
	_set_referrer(doc, parent=parent, customer=customer, actor=actor)
	doc.save(ignore_permissions=True)
	_add_referral_comment(
		doc,
		_("Historical referral recognised and linked to Parent {0}. No historical Trial Invoice was changed.").format(parent),
	)
	return award_referral_conversion_reward(doc, enrollment=doc.get("converted_enrollment"), actor=actor)


def award_referral_conversion_reward(inquiry_doc, *, enrollment: str | None = None, actor=None):
	"""Create one traceable referral reward ledger entry and queue its parent email."""
	inquiry_name = str(getattr(inquiry_doc, "name", "") or "").strip()
	if not inquiry_name:
		frappe.throw(_("Inquiry is required for a referral reward."))
	# The conversion and the historical-recognition action may be clicked twice or
	# retried by the browser.  Hold one short lock around the lookup and insert so
	# they can never create two referral credits for the same Inquiry.
	with frappe.cache.lock("qas-referral-reward:{0}".format(inquiry_name), timeout=30, blocking_timeout=10):
		return _award_referral_conversion_reward(inquiry_doc, enrollment=enrollment, actor=actor)


def _award_referral_conversion_reward(inquiry_doc, *, enrollment: str | None = None, actor=None):
	result = {
		"created": False,
		"already_exists": False,
		"skipped": True,
		"reason": None,
		"amount": 0,
		"entry": None,
		"notification": None,
	}
	if not verified_trial_referral(inquiry_doc):
		result["reason"] = "Referral is not verified."
		return result
	if inquiry_doc.get("status") != "Converted":
		result["reason"] = "Referral reward is available only after conversion."
		return result

	amount = referral_conversion_reward_amount()
	result["amount"] = amount
	if amount <= 0:
		result["reason"] = "Referral conversion reward is set to zero."
		return result

	existing = _existing_referral_reward(inquiry_doc.name)
	if existing:
		_set_if_field(inquiry_doc, "referral_reward_ledger", existing.get("name"))
		inquiry_doc.save(ignore_permissions=True)
		result.update({"already_exists": True, "reason": "Referral reward already exists.", "entry": existing})
		return result

	parent = verified_referral_parent(inquiry_doc)
	customer = str(inquiry_doc.get("referring_customer") or "").strip() or get_invoice_customer(parent)
	entry = create_store_credit_entry(
		parent=parent,
		customer=customer,
		transaction_type=REFERRAL_REWARD_TYPE,
		credit_amount=amount,
		enrollment=enrollment or inquiry_doc.get("converted_enrollment"),
		reference_doctype="Inquiry",
		reference_document=inquiry_doc.name,
		source_doctype="Inquiry",
		source_document=inquiry_doc.name,
		reason=_("Referral conversion reward"),
		notes=_("Referral reward granted after Inquiry {0} converted.").format(inquiry_doc.name),
	)
	_set_if_field(inquiry_doc, "referring_customer", customer)
	_set_if_field(inquiry_doc, "referral_reward_ledger", entry.name)
	inquiry_doc.save(ignore_permissions=True)
	_add_referral_comment(
		inquiry_doc,
		_("Referral reward of {0} Store Credit granted to Parent {1}.").format(
			frappe.format_value(amount, {"fieldtype": "Currency"}),
			parent,
		),
	)

	from qas_custom.modules.notifications.trial_referral_notifications import (
		queue_referral_conversion_reward_notification,
	)

	result.update(
		{
			"created": True,
			"skipped": False,
			"entry": entry.as_dict(),
			"notification": queue_referral_conversion_reward_notification(inquiry_doc.name),
		}
	)
	return result


def referral_invoice_message(inquiry_doc) -> str:
	"""Keep the normal invoice copy, then state the verified referral benefit clearly."""
	settings = get_invoice_settings()
	base_message = str(settings.get("invoice_message") or "").strip()
	if not verified_trial_referral(inquiry_doc):
		return base_message
	referrer_name = _parent_display_name(verified_referral_parent(inquiry_doc))
	discount = trial_referral_discount_amount()
	benefit = _(
		"You were referred by {0} and have received a {1} trial-class discount."
	).format(referrer_name, frappe.format_value(discount, {"fieldtype": "Currency"}))
	return "\n\n".join(part for part in [base_message, benefit] if part)


def referral_summary(inquiry_doc) -> dict:
	status = referral_status(inquiry_doc)
	referrer = verified_referral_parent(inquiry_doc)
	reward = _existing_referral_reward(inquiry_doc.name)
	return {
		"is_referral_claim": is_referral_claim(inquiry_doc),
		"status": status,
		"referral_detail": inquiry_doc.get("referral_detail") or "",
		"referring_parent": referrer,
		"referring_parent_name": _parent_display_name(referrer) if referrer else "",
		"referring_customer": inquiry_doc.get("referring_customer") or "",
		"verified_by": inquiry_doc.get("referral_verified_by") or "",
		"verified_at": str(inquiry_doc.get("referral_verified_at") or ""),
		"trial_discount": trial_referral_discount_amount(),
		"conversion_reward": referral_conversion_reward_amount(),
		"reward_ledger": (reward or {}).get("name") or inquiry_doc.get("referral_reward_ledger") or "",
		"reward_amount": flt((reward or {}).get("credit_amount") or 0),
		"can_verify": bool(is_referral_claim(inquiry_doc) and status == REFERRAL_PENDING and inquiry_doc.get("status") != "Converted"),
		"can_release_standard_invoice": bool(is_referral_claim(inquiry_doc) and status == REFERRAL_PENDING and inquiry_doc.get("status") != "Converted"),
		"can_recognise_historical": bool(inquiry_doc.get("inquiry_type") == "Trial Lesson" and inquiry_doc.get("status") == "Converted" and not reward),
	}


def _get_trial_inquiry(inquiry: str):
	if not inquiry:
		frappe.throw(_("Inquiry is required."))
	doc = frappe.get_doc("Inquiry", inquiry)
	if doc.get("inquiry_type") != "Trial Lesson":
		frappe.throw(_("Referral actions are available only for Trial Lesson inquiries."))
	return doc


def _validated_referrer(inquiry_doc, referring_parent: str):
	parent = str(referring_parent or "").strip()
	if not parent or not frappe.db.exists("Parent", parent):
		frappe.throw(_("Choose the existing referring parent."))
	if parent == inquiry_doc.get("parent"):
		frappe.throw(_("The referred family cannot refer itself."))
	customer = frappe.db.get_value("Parent", parent, "customer") if frappe.db.has_column("Parent", "customer") else None
	return parent, str(customer or get_invoice_customer(parent)).strip()


def _set_referrer(doc, *, parent: str, customer: str, actor=None):
	_set_if_field(doc, "referral_status", REFERRAL_VERIFIED)
	_set_if_field(doc, "referring_parent", parent)
	_set_if_field(doc, "referring_customer", customer)
	_set_if_field(doc, "referral_verified_by", actor or frappe.session.user)
	_set_if_field(doc, "referral_verified_at", now_datetime())


def _resume_status(doc):
	stored = str(doc.get("referral_resume_status") or "").strip()
	if stored in {"Booked", "Rescheduled", "New"}:
		return stored
	return "Booked" if doc.get("course_session") else "New"


def _existing_referral_reward(inquiry: str):
	if not inquiry or not frappe.db.exists("DocType", LEDGER_DOCTYPE):
		return None
	return frappe.db.get_value(
		LEDGER_DOCTYPE,
		{
			"transaction_type": REFERRAL_REWARD_TYPE,
			"source_doctype": "Inquiry",
			"source_document": inquiry,
		},
		["name", "credit_amount", "customer", "parent", "creation"],
		as_dict=True,
	)


def _parent_display_name(parent: str) -> str:
	if not parent:
		return _("a Queensland Art School family")
	if frappe.db.has_column("Parent", "parent_name"):
		return frappe.db.get_value("Parent", parent, "parent_name") or parent
	return parent


def _set_if_field(doc, fieldname: str, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _add_referral_comment(doc, message: str):
	try:
		doc.add_comment("Info", message)
	except Exception:
		pass
