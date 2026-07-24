from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


MAKEUP_PRICE_DIFFERENCE_SOURCE_TYPE = "Makeup Price Difference"


def classify_makeup_target(*, source_course, target_course, accepted_courses=None):
	"""Return the School Admin booking policy and current per-session price effect."""
	source_name = _value(source_course, "name")
	target_name = _value(target_course, "name")
	if not source_name or not target_name:
		raise ValueError("Source and target courses are required.")

	if target_name == source_name:
		return _price_plan(
			classification="same_course",
			source_course=source_name,
			target_course=target_name,
			source_fee=_value(source_course, "term_session_fee"),
			target_fee=_value(target_course, "term_session_fee"),
			price_difference=0,
		)

	if int(_value(target_course, "is_makeup_course") or 0):
		accepted = {str(course) for course in (accepted_courses or []) if course}
		if source_name not in accepted:
			raise ValueError(
				_("Dedicated makeup course {0} does not accept vouchers from {1}.").format(
					target_name,
					source_name,
				)
			)
		return _price_plan(
			classification="dedicated_makeup_course",
			source_course=source_name,
			target_course=target_name,
			source_fee=None,
			target_fee=None,
			price_difference=0,
		)

	source_fee = flt(_value(source_course, "term_session_fee"))
	target_fee = flt(_value(target_course, "term_session_fee"))
	if source_fee <= 0:
		raise ValueError(
			_("Source course {0} needs a current Term Session Fee before cross-course makeup can be booked.").format(
				source_name
			)
		)
	if target_fee <= 0:
		raise ValueError(
			_("Target course {0} needs a current Term Session Fee before cross-course makeup can be booked.").format(
				target_name
			)
		)
	difference = max(round(target_fee - source_fee, 2), 0)
	return _price_plan(
		classification="ordinary_cross_course",
		source_course=source_name,
		target_course=target_name,
		source_fee=source_fee,
		target_fee=target_fee,
		price_difference=difference,
	)


def get_makeup_target_pricing(source_course: str, target_course: str):
	try:
		return preview_makeup_target_pricing(source_course, target_course)
	except ValueError as exc:
		frappe.throw(str(exc))


def preview_makeup_target_pricing(source_course: str, target_course: str):
	source = _get_course_pricing_context(source_course)
	target = _get_course_pricing_context(target_course)
	accepted_courses = _get_accepted_makeup_courses(target_course) if int(target.get("is_makeup_course") or 0) else []
	return classify_makeup_target(
		source_course=source,
		target_course=target,
		accepted_courses=accepted_courses,
	)


def classify_difference_invoice(invoice):
	if not invoice:
		return {"action": "none", "upgrade_voucher_course": False}

	docstatus = int(_value(invoice, "docstatus") or 0)
	status = str(_value(invoice, "status") or "").strip().lower()
	paid_amount = flt(_value(invoice, "paid_amount"))
	outstanding = flt(_value(invoice, "outstanding_amount"))

	if docstatus == 0:
		return {"action": "delete_draft", "upgrade_voucher_course": False}
	if docstatus == 2 or status == "cancelled":
		return {"action": "release_cancelled", "upgrade_voucher_course": False}
	if docstatus == 1 and outstanding <= 0:
		return {"action": "keep_paid", "upgrade_voucher_course": True}
	if docstatus == 1 and paid_amount > 0:
		return {"action": "block_partial", "upgrade_voucher_course": False}
	return {"action": "block_unpaid", "upgrade_voucher_course": False}


def get_makeup_difference_invoice(voucher):
	voucher_name = _value(voucher, "name")
	linked_invoice = _value(voucher, "price_difference_invoice")
	fields = ["name", "docstatus", "status", "grand_total", "paid_amount", "outstanding_amount"]
	if linked_invoice and frappe.db.exists("Sales Invoice", linked_invoice):
		return frappe.db.get_value("Sales Invoice", linked_invoice, fields, as_dict=True)
	has_column = getattr(frappe.db, "has_column", None)
	if has_column and has_column("Makeup Voucher", "price_difference_invoice"):
		return None

	if not voucher_name:
		return None
	filters = {
		"source_doctype": "Makeup Voucher",
		"source_document": voucher_name,
		"source_type": MAKEUP_PRICE_DIFFERENCE_SOURCE_TYPE,
	}
	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=fields,
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def _get_course_pricing_context(course: str):
	if not course:
		raise ValueError(_("Course is required."))
	row = frappe.db.get_value(
		"Course",
		course,
		["name", "is_makeup_course", "term_session_fee"],
		as_dict=True,
	)
	if not row:
		raise ValueError(_("Course was not found: {0}").format(course))
	return row


def _get_accepted_makeup_courses(course: str):
	if not course:
		return []
	return [
		row.get("course")
		for row in frappe.get_all(
			"Course Accepted Makeup Course",
			filters={
				"parent": course,
				"parenttype": "Course",
				"parentfield": "accepted_makeup_course",
			},
			fields=["course"],
			order_by="idx asc",
			limit_page_length=0,
		)
		if row.get("course")
	]


def _price_plan(
	*,
	classification,
	source_course,
	target_course,
	source_fee,
	target_fee,
	price_difference,
):
	difference = max(flt(price_difference), 0)
	return {
		"classification": classification,
		"source_course": source_course,
		"target_course": target_course,
		"source_session_fee": flt(source_fee) if source_fee not in (None, "") else None,
		"target_session_fee": flt(target_fee) if target_fee not in (None, "") else None,
		"price_difference": difference,
		"requires_difference_invoice": difference > 0,
	}


def _value(row, key, default=None):
	if hasattr(row, "get"):
		return row.get(key, default)
	return getattr(row, key, default)
