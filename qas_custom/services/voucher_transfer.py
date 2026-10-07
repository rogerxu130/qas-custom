"""School Admin transfers of unused vouchers between equal-price ordinary courses."""
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
import json

import frappe
from frappe import _
from frappe.utils import getdate, today

from qas_custom.modules.makeup.pricing import get_makeup_difference_invoice
from qas_custom.services.display_labels import sync_makeup_voucher_label
from qas_custom.services.school_admin import (
    _require_school_admin, _get_school_admin_family_context,
    _get_school_admin_voucher_family_context, _school_admin_makeup_lock,
    _school_admin_required_reason,
)


def fee_cents(course):
    try:
        fee = Decimal(str(course.get("term_session_fee") or 0))
        if not fee.is_finite() or fee <= 0 or course.get("is_makeup_course"):
            return None
        return int((fee * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def assert_equal_price(source, target):
    source_fee, target_fee = fee_cents(source), fee_cents(target)
    if source.get("name") == target.get("name"):
        frappe.throw(_("Choose a different target course."))
    if target.get("status") != "Active":
        frappe.throw(_("The target course must be active."))
    if source_fee is None or source_fee != target_fee:
        frappe.throw(_("Both ordinary courses must have the same positive Term Session Fee."))


def voucher_available(voucher):
    return (
        voucher.get("status") == "Valid"
        and bool(voucher.get("expiry_date"))
        and getdate(voucher.get("expiry_date")) >= getdate(today())
        and not any(voucher.get(field) for field in ("used_on_session", "used_date", "used_by_student"))
    )


def assert_available(voucher):
    if not voucher_available(voucher):
        frappe.throw(_("Voucher {0} is expired, used or already booked.").format(voucher.name))
    invoice = get_makeup_difference_invoice(voucher)
    if invoice and int(invoice.get("docstatus") or 0) != 2:
        frappe.throw(_("Voucher {0} has a price difference invoice that must be resolved first.").format(voucher.name))


def get_transfer_options(parent=None):
    _require_school_admin()
    if not parent:
        frappe.throw(_("Parent is required."))
    parent_doc, students = _get_school_admin_family_context(parent=parent)
    vouchers = frappe.get_all("Makeup Voucher", filters={"student": ["in", [s.get("name") for s in students]], "status": "Valid"}, fields=["name", "student", "course", "expiry_date", "status", "used_on_session", "used_date"], order_by="creation desc", limit_page_length=0)
    courses = frappe.get_all("Course", fields=["name", "course_name", "status", "term_session_fee", "is_makeup_course"], limit_page_length=0)
    prices = {row.name: fee_cents(row) for row in courses}
    eligible = []
    for row in vouchers:
        if not voucher_available(row) or prices.get(row.course) is None:
            continue
        invoice = get_makeup_difference_invoice(row)
        if invoice and int(invoice.get("docstatus") or 0) != 2:
            continue
        eligible.append(row)
    return {"vouchers": eligible, "courses": [{"name": row.name, "label": row.get("course_name") or row.name, "fee_cents": prices[row.name], "status": row.status} for row in courses if prices[row.name] is not None]}


def transfer_vouchers(parent=None, voucher_ids=None, source_course=None, target_course=None, reason=None):
    _require_school_admin()
    reason = _school_admin_required_reason(reason)
    if isinstance(voucher_ids, str):
        try:
            voucher_ids = json.loads(voucher_ids)
        except (ValueError, TypeError):
            frappe.throw(_("Voucher IDs must be a JSON list."))
    if not parent or not isinstance(voucher_ids, list) or not voucher_ids or len(voucher_ids) > 200 or any(not isinstance(v, str) or not v.strip() for v in voucher_ids):
        frappe.throw(_("Select between 1 and 200 vouchers for this family."))
    if not source_course or not target_course:
        frappe.throw(_("Source and target courses are required."))
    ids = sorted(set(voucher_ids))
    frappe.db.savepoint("voucher_transfer")
    try:
        with ExitStack() as locks:
            for name in ids:
                locks.enter_context(_school_admin_makeup_lock(name))
            # Lock prices as well as vouchers so an edit cannot change equality mid-transfer.
            courses = {}
            for name in sorted(set([source_course, target_course])):
                courses[name] = frappe.get_doc("Course", name, for_update=True)
            assert_equal_price(courses[source_course], courses[target_course])
            vouchers = []
            for name in ids:
                voucher = frappe.get_doc("Makeup Voucher", name, for_update=True)
                parent_doc, students, family_voucher = _get_school_admin_voucher_family_context(parent=parent, voucher_id=name)
                if voucher.student not in {s.get("name") for s in students}:
                    frappe.throw(_("Voucher does not belong to this family."), frappe.PermissionError)
                assert_available(voucher)
                if voucher.course != source_course:
                    frappe.throw(_("Voucher {0} changed course. Refresh and try again.").format(name))
                vouchers.append(voucher)
            for voucher in vouchers:
                voucher.course = target_course
                voucher.save(ignore_permissions=True)
                sync_makeup_voucher_label(voucher)
                voucher.add_comment("Comment", _("Course transferred: {0} → {1}. Reason: {2}").format(source_course, target_course, reason))
        return {"transferred": len(ids), "voucher_ids": ids, "source_course": source_course, "target_course": target_course}
    except Exception:
        frappe.db.rollback(save_point="voucher_transfer")
        raise
