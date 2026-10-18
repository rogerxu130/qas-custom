"""Order-specific billing. No tuition credit, implicit refunds or intermediate commits."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

ORDER_TYPE = "Store Order"


def is_order_invoice(doc):
    return doc.get("qas_invoice_type") in {ORDER_TYPE, "Material Order"} or doc.get("source_doctype") == ORDER_TYPE


def money(value):
    try:
        value = Decimal(str(value or 0))
        if not value.is_finite():
            raise ValueError()
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        frappe.throw(_("Enter a valid amount."))


def totals(items, discount_type="", discount_value=0, discount_reason=""):
    subtotal = money(sum(Decimal(str(row.get("unit_price") or 0)) * int(row.get("qty")) for row in items))
    value = money(discount_value)
    if value < 0 or discount_type not in {"", "Amount", "Percent"}:
        frappe.throw(_("Choose a valid discount."))
    if value and (not discount_type or not str(discount_reason or "").strip()):
        frappe.throw(_("A discount type and reason are required."))
    if discount_type == "Percent" and value > 100:
        frappe.throw(_("Discount percentage cannot exceed 100."))
    discount = money(subtotal * value / 100) if discount_type == "Percent" else value
    if discount > subtotal:
        frappe.throw(_("Discount cannot exceed the order subtotal."))
    return {"subtotal": subtotal, "discount_amount": discount, "order_total": money(subtotal - discount)}


def _scope(write=False):
    from qas_custom.services.support_view import reject_support_view_write, get_support_view_campus_admin_profile
    if write:
        reject_support_view_write()
    if not get_support_view_campus_admin_profile() and {"School Admin", "System Manager"}.intersection(frappe.get_roles()):
        return None
    from qas_custom.services.campus_admin import _require_campus_admin_profile
    return _require_campus_admin_profile()["campuses"]


def _access(doc, scope):
    if scope is not None and doc.pickup_campus not in scope:
        frappe.throw(_("This order is outside your assigned campuses."), frappe.PermissionError)


def _lock(name, scope):
    frappe.db.sql("SELECT name FROM `tabStore Order` WHERE name=%s FOR UPDATE", (name,))
    doc = frappe.get_doc(ORDER_TYPE, name)
    _access(doc, scope)
    # Lock invoices before inspecting payment/outstanding or preparing a confirmation.
    frappe.db.sql("SELECT name FROM `tabSales Invoice` WHERE source_doctype=%s AND source_document=%s ORDER BY name FOR UPDATE", (ORDER_TYPE, name))
    return doc


def invoice_rows(doc):
    return frappe.get_all("Sales Invoice", filters={"source_doctype": ORDER_TYPE, "source_document": doc.name},
        fields=["name", "docstatus", "grand_total", "outstanding_amount", "is_return", "amended_from", "posting_date", "modified"],
        order_by="creation asc", limit_page_length=0)


def financials(doc):
    rows = invoice_rows(doc)
    names = [r.name for r in rows if cint(r.docstatus) == 1]
    paid = 0
    if names:
        # Payment Entries, not face-value minus outstanding: Credit Notes also change outstanding.
        paid = frappe.db.sql("""SELECT COALESCE(SUM(CASE WHEN p.payment_type='Receive' THEN r.allocated_amount ELSE -r.allocated_amount END),0)
            FROM `tabPayment Entry Reference` r JOIN `tabPayment Entry` p ON p.name=r.parent
            WHERE p.docstatus=1 AND r.reference_doctype='Sales Invoice' AND r.reference_name IN %(names)s""", {"names": names})[0][0]
        # Support actual ERP POS receipts too, without manufacturing a payment or refund.
        paid += sum(flt(frappe.db.get_value("Sales Invoice", name, "paid_amount")) for name in names)
    amount = totals(doc.get("items") or [], doc.get("discount_type") or "", doc.get("discount_value"), doc.get("discount_reason"))["order_total"]
    if doc.status == "Cancelled":
        amount = 0
    active = [r for r in rows if cint(r.docstatus) == 1]
    draft = any(cint(r.docstatus) == 0 for r in rows)
    status = "Cancelled" if doc.status == "Cancelled" else "Draft" if draft or not active else "Paid" if money(paid) >= amount else "Issued"
    return {"invoice_status": status, "paid_amount": money(paid), "remaining_amount": max(0, money(amount - paid)), "invoices": rows}


def order_payload(doc, parent=False):
    from qas_custom.services.material_orders import _order_payload
    result = _order_payload(doc)
    result.update(totals(doc.get("items") or [], doc.get("discount_type") or "", doc.get("discount_value"), doc.get("discount_reason")))
    result.update(financials(doc))
    if doc.status == "Cancelled":
        result["order_total"] = 0
    result.update({key: doc.get(key) for key in ("discount_type", "discount_value", "discount_reason", "created_by", "invoice_notification_error", "invoice_email_queue")})
    from qas_custom.modules.notifications.store_order_notifications import notification_status
    invoice_mail = notification_status(frappe._dict(ready_email_queue=doc.get("invoice_email_queue"), ready_notification_error=doc.get("invoice_notification_error")))
    result["invoice_notification_status"] = invoice_mail["status"]
    result["invoice_notification_error"] = invoice_mail["error"]
    result["pending_approval"] = bool(doc.get("pending_revision"))
    if doc.get("pending_revision") and not parent:
        result["pending_revision"] = json.loads(doc.pending_revision)
    if parent:
        result["invoices"] = [row for row in result["invoices"] if cint(row.docstatus) != 0]
        result.pop("cancellation_reason", None)
    result["status"] = "Dispatched" if doc.status == "Collected" else doc.status
    return result


def _items(rows, existing=None):
    from qas_custom.services.material_orders import _store_order_items
    result = _store_order_items(rows, existing_rows=existing.get("items") if existing else None)
    snapshots = {row.store_product: row for row in (existing.get("items") or [])} if existing else {}
    for row in result:
        if row["store_product"] in snapshots:
            old = snapshots[row["store_product"]]
            row["unit_price"] = flt(old.unit_price)
            row["product_name"] = old.product_name
            row["amount"] = money(row["qty"] * row["unit_price"])
    return result


def _changes(doc, data, scope):
    from qas_custom.services.material_orders import _store_order_options
    campus = str(data.get("pickup_campus") or doc.pickup_campus)
    choices = _store_order_options(frappe.get_doc("Parent", doc.parent), admin=True)["pickup_campuses"]
    if campus not in {row.name for row in choices} or scope is not None and campus not in scope:
        frappe.throw(_("Choose a collection campus you can access."), frappe.PermissionError)
    result = {"items": _items(data.get("items"), doc), "pickup_campus": campus,
        "discount_type": data.get("discount_type") or "", "discount_value": money(data.get("discount_value")),
        "discount_reason": str(data.get("discount_reason") or "").strip(), "reason": str(data.get("reason") or "").strip()}
    if not result["reason"]:
        frappe.throw(_("Enter a reason for this change."))
    totals(result["items"], result["discount_type"], result["discount_value"], result["discount_reason"])
    return result


def _apply(doc, changes):
    changed_items = [(r.store_product, cint(r.qty)) for r in doc.get("items") or []] != [(r["store_product"], r["qty"]) for r in changes["items"]]
    doc.set("items", changes["items"])
    for field in ("pickup_campus", "discount_type", "discount_value", "discount_reason"):
        doc.set(field, changes[field])
    if changed_items and doc.status == "Ready for collection":
        doc.status = "Ordered"
        doc.ready_email_queue = None
        doc.ready_notification_error = ""
        doc.ready_notification_skipped = 0
    doc.save(ignore_permissions=True)


def _mutation(callback):
    from qas_custom.modules.billing.commands import run_invoice_mutation_as_administrator
    return run_invoice_mutation_as_administrator(callback)


def _fill_invoice(invoice, doc, adjustment=None):
    from qas_custom.modules.common import set_if_field
    invoice.set("items", [])
    for row in doc.get("items") or []:
        if not row.item_code:
            from qas_custom.services.material_orders import _ensure_material_item, _get_product
            row.item_code = _ensure_material_item(_get_product(row.store_product))
    if adjustment is None:
        for row in doc.get("items") or []:
            item = invoice.append("items", {"item_code": row.item_code, "item_name": row.product_name,
                "description": row.product_name, "qty": row.qty, "rate": row.unit_price})
            set_if_field(item, "qas_line_type", "Material")
        amounts = totals(doc.get("items") or [], doc.get("discount_type") or "", doc.get("discount_value"), doc.get("discount_reason"))
        invoice.apply_discount_on = "Grand Total"
        invoice.discount_amount = amounts["discount_amount"]
        invoice.additional_discount_percentage = 0
    else:
        # Standalone Credit Note avoids returning fictitious quantities from already delivered goods.
        row = (doc.get("items") or [])[0]
        item = invoice.append("items", {"item_code": row.item_code, "item_name": "Order price adjustment",
            "description": f"Order {doc.name}: {doc.get('revision_reason') or 'price / quantity adjustment'}",
            "qty": -1 if adjustment < 0 else 1, "rate": abs(adjustment)})
        set_if_field(item, "qas_line_type", "Material")
        invoice.is_return = int(adjustment < 0)
    invoice.taxes_and_charges = None
    invoice.set("taxes", [])  # Store prices are final, including any applicable GST presentation.
    invoice.disable_rounded_total = 1
    invoice.update_stock = 0
    invoice.allocate_advances_automatically = 0
    invoice.set("advances", [])
    invoice.remarks = f"Order Invoice for {doc.name}. {doc.get('revision_reason') or ''}"


def _new_invoice(doc, adjustment=None, amended_from=None):
    from qas_custom.modules.billing.invoice_settings import apply_default_invoice_dates, apply_invoice_payment_snapshot
    from qas_custom.modules.common import set_if_field
    from qas_custom.modules.notifications.guard import disable_sales_invoice_auto_notifications
    disable_sales_invoice_auto_notifications()
    invoice = frappe.new_doc("Sales Invoice")
    invoice.customer = doc.customer
    apply_default_invoice_dates(invoice)
    for field, value in {"parent": doc.parent, "qas_invoice_type": ORDER_TYPE, "source_doctype": ORDER_TYPE,
            "source_document": doc.name, "source_type": ORDER_TYPE, "qas_is_manual_invoice": 0,
            "qas_apply_store_credit_on_submit": 0}.items():
        set_if_field(invoice, field, value)
    invoice.amended_from = amended_from
    apply_invoice_payment_snapshot(invoice)
    _fill_invoice(invoice, doc, adjustment)
    _mutation(lambda: invoice.insert(ignore_permissions=True))
    return invoice


def _notify(doc, invoice=None):
    from qas_custom.modules.notifications.store_order_notifications import queue_order_invoice_notification
    queue_order_invoice_notification(doc, invoice)


def _issue_draft(doc, *, invoice_name=None, send_notifications=True):
    if doc.status == "Cancelled" or doc.get("pending_revision"):
        frappe.throw(_("Review this order's cancellation or pending revision before issuing."))
    if not doc.invoice or invoice_name and doc.invoice != invoice_name:
        frappe.throw(_("The order invoice changed. Refresh and open its current invoice."))
    invoice = frappe.get_doc("Sales Invoice", doc.invoice)
    if (invoice.get("source_doctype"), invoice.get("source_document")) != (ORDER_TYPE, doc.name):
        frappe.throw(_("This invoice is not linked to its order."))
    if cint(invoice.docstatus) != 0:
        frappe.throw(_("This invoice has already been issued."))
    _mutation(invoice.submit)
    doc.add_comment("Comment", f"Invoice issued by {frappe.session.user}")
    if send_notifications:
        _notify(doc, invoice)
    return invoice


def submit_order_invoice(invoice_name, *, send_notifications=True):
    """Use the order lock and billing rules for the unified invoice queue."""
    if _scope(write=True) is not None:
        frappe.throw(_("School Admin approval is required."), frappe.PermissionError)
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    if invoice.get("source_doctype") != ORDER_TYPE or not invoice.get("source_document"):
        frappe.throw(_("This Order Invoice has no linked order to issue."))
    doc = _lock(invoice.source_document, None)
    _issue_draft(doc, invoice_name=invoice_name, send_notifications=send_notifications)
    if not send_notifications:
        return {"skipped": True, "reason": "Parent notifications were skipped for this submission."}
    # Read the result of db_set without assuming that queued mail has been delivered.
    doc.reload()
    from qas_custom.modules.notifications.store_order_notifications import notification_status
    status = notification_status(frappe._dict(ready_email_queue=doc.get("invoice_email_queue"), ready_notification_error=doc.get("invoice_notification_error")))
    return {"sent": status["status"] == "Sent", "queued": status["status"] in {"Queued", "Sending"}, "reason": status.get("error") or status["status"]}


def create_invoice(doc, issue=False):
    from qas_custom.services.parent_customer import ensure_parent_customer
    parent = frappe.get_doc("Parent", doc.parent)
    doc.customer = ensure_parent_customer(parent)
    if not doc.customer:
        frappe.throw(_("A Customer account could not be created for this family."))
    invoice = _new_invoice(doc)
    doc.invoice = invoice.name
    doc.save(ignore_permissions=True)
    if issue:
        _mutation(invoice.submit)
        _notify(doc, invoice)
    return invoice


def _revise(doc, changes):
    before = financials(doc)
    old_total = totals(doc.get("items") or [], doc.get("discount_type") or "", doc.get("discount_value"), doc.get("discount_reason"))["order_total"]
    _apply(doc, changes)
    new_total = totals(changes["items"], changes["discount_type"], changes["discount_value"], changes["discount_reason"])["order_total"]
    doc.revision_reason = changes["reason"]
    if not doc.invoice:
        # Historical pickup-only orders are not silently back-billed.
        return
    current = frappe.get_doc("Sales Invoice", doc.invoice)
    if before["paid_amount"] <= 0 and len([r for r in before["invoices"] if cint(r.docstatus) == 1]) <= 1:
        if cint(current.docstatus) == 0:
            _fill_invoice(current, doc)
            _mutation(lambda: current.save(ignore_permissions=True))
        else:
            _mutation(current.cancel)
            revised = _new_invoice(doc, amended_from=current.name)
            _mutation(revised.submit)
            doc.invoice = revised.name
            _notify(doc, revised)
    else:
        delta = money(new_total - old_total)
        if delta:
            adjustment = _new_invoice(doc, adjustment=delta)
            _mutation(adjustment.submit)
        _notify(doc)
    doc.add_comment("Comment", _("Order revised by {0}: {1}").format(frappe.session.user, changes["reason"]))
    doc.save(ignore_permissions=True)


def _token(doc, changes):
    state = {"modified": str(doc.modified), "finance": financials(doc), "changes": changes}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _preview(doc, changes):
    amounts = totals(changes["items"], changes["discount_type"], changes["discount_value"], changes["discount_reason"])
    paid = financials(doc)["paid_amount"]
    return {**amounts, "paid_amount": paid, "remaining_amount": max(0, money(amounts["order_total"] - paid)),
        "refund_reminder": max(0, money(paid - amounts["order_total"])), "confirmation": _token(doc, changes)}


def _confirm(doc, changes, confirmation):
    if not confirmation or confirmation != _token(doc, changes):
        frappe.throw(_("The order or payments changed. Review the latest amounts and confirm again."))


def _cancel(doc, reason):
    before = financials(doc)
    if before["paid_amount"] > 0:
        amount = totals(doc.get("items") or [], doc.get("discount_type") or "", doc.get("discount_value"), doc.get("discount_reason"))["order_total"]
        if amount:
            credit = _new_invoice(doc, adjustment=-amount)
            _mutation(credit.submit)
    else:
        for row in before["invoices"]:
            invoice = frappe.get_doc("Sales Invoice", row.name)
            if cint(invoice.docstatus) == 1:
                _mutation(invoice.cancel)
            # Keep a cancelled order's original Draft as history; it cannot be issued or paid.
    doc.status = "Cancelled"
    doc.cancelled_at = now_datetime()
    doc.cancelled_by = frappe.session.user
    doc.cancellation_reason = reason
    doc.pending_revision = None
    doc.save(ignore_permissions=True)
    doc.add_comment("Comment", f"Cancelled by {frappe.session.user}: {reason}")
    _notify(doc)


def _families(scope, query="", exact_parent=None):
    query = str(query or "").strip()
    if scope is not None and not exact_parent:
        if len(query) < 2 or any(char in query for char in "%_*?\\"):
            return []
        from qas_custom.services.campus_admin import get_campus_admin_current_term_enrollments_data
        result = get_campus_admin_current_term_enrollments_data(query=query)
        if result.get("state", "ready") != "ready":
            frappe.throw(result.get("message") or _("Student search is unavailable. Please try again."))
        matches = result["items"]
        students = list({row["student"] for row in matches})
        if not students:
            return []
        parents = frappe.get_all("Student", filters={"name": ["in", students]}, pluck="guardian", limit_page_length=0)
        return frappe.get_all("Parent", filters={"name": ["in", parents]}, fields=["name", "parent_name"], order_by="parent_name,name", limit_page_length=50)
    conditions, values = [], {"query": "%" + query + "%"}
    if scope is not None:
        # Include enrolled families and linked enquiries at an assigned campus.
        conditions.append("""(EXISTS(SELECT 1 FROM `tabEnrollment` e JOIN `tabWeekly Timeslot` w ON w.name=e.weekly_timeslot WHERE e.parent=p.name AND w.campus IN %(campuses)s)
            OR EXISTS(SELECT 1 FROM `tabInquiry` i WHERE i.parent=p.name AND i.campus IN %(campuses)s)
            OR EXISTS(SELECT 1 FROM `tabStore Order` o WHERE o.parent=p.name AND o.pickup_campus IN %(campuses)s))""")
        values["campuses"] = scope
    if exact_parent:
        conditions.append("p.name=%(exact_parent)s")
        values["exact_parent"] = exact_parent
    elif query:
        from qas_custom.services.school_admin import _matching_student_parent_ids
        fields = [name for name in ("name", "parent_name", "mobile_number", "phone", "email", "email_id", "linked_user", "customer") if name == "name" or frappe.get_meta("Parent").has_field(name)]
        search = [f"p.`{name}` LIKE %(query)s" for name in fields]
        matched_parents = _matching_student_parent_ids(query)
        if matched_parents:
            values["matched_parents"] = matched_parents
            search.append("p.name IN %(matched_parents)s")
        conditions.append("(" + " OR ".join(search) + ")")
    where = " AND ".join(conditions) or "1=1"
    return frappe.db.sql(f"SELECT p.name,p.parent_name FROM `tabParent` p WHERE {where} ORDER BY p.parent_name,p.name LIMIT 100", values, as_dict=True)


def _family_access(parent, scope):
    if scope is not None:
        if parent not in {row.name for row in _families(scope, exact_parent=parent)}:
            frappe.throw(_("This family is outside your assigned campuses."), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def manage(action=None, payload=None):
    data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
    if not isinstance(data, dict):
        frappe.throw(_("Invalid order request."))
    read_actions = {"list", "get", "catalog", "families", "options", "preview", "preview_cancel", "preview_approval"}
    scope = _scope(write=action not in read_actions)
    if action == "catalog":
        from qas_custom.services.material_orders import _product_payload
        rows = frappe.get_all("Store Product", filters={"active": 1}, pluck="name", order_by="product_name", limit_page_length=0)
        return {"items": [_product_payload(frappe.get_doc("Store Product", name), include_media=False) for name in rows], "can_issue": scope is None, "payment_methods": frappe.get_all("Mode of Payment", filters={"enabled": 1}, pluck="name", order_by="name", limit_page_length=0)}
    if action == "families":
        return {"items": _families(scope, data.get("query") or "")}
    if action == "options":
        from qas_custom.services.material_orders import _store_order_options
        _family_access(data.get("parent"), scope)
        result = _store_order_options(frappe.get_doc("Parent", data.get("parent")), admin=True)
        if scope is not None:
            result["pickup_campuses"] = [r for r in result["pickup_campuses"] if r.name in scope]
        return result
    if action == "list":
        conditions, values = [], {"start": max(0, cint(data.get("start"))), "limit": 81}
        if scope is not None:
            conditions.append("o.pickup_campus IN %(campuses)s"); values["campuses"] = scope
        if data.get("campus"):
            conditions.append("o.pickup_campus=%(campus)s"); values["campus"] = data["campus"]
        if data.get("status"):
            conditions.append("o.status=%(status)s"); values["status"] = "Collected" if data["status"] == "Dispatched" else data["status"]
        if data.get("query"):
            conditions.append("(o.name LIKE %(q)s OR EXISTS(SELECT 1 FROM `tabParent` p WHERE p.name=o.parent AND p.parent_name LIKE %(q)s) OR EXISTS(SELECT 1 FROM `tabSales Invoice` s WHERE s.source_doctype='Store Order' AND s.source_document=o.name AND s.name LIKE %(q)s) OR EXISTS(SELECT 1 FROM `tabStore Order Item` i WHERE i.parent=o.name AND i.parenttype='Store Order' AND i.product_name LIKE %(q)s))")
            values["q"] = "%" + str(data["query"]).strip() + "%"
        if data.get("workspace") == "invoices":
            conditions.append("o.invoice IS NOT NULL AND o.invoice!=''")
        where = " AND ".join(conditions) or "1=1"
        rows = frappe.db.sql(f"SELECT o.name FROM `tabStore Order` o WHERE {where} ORDER BY o.modified DESC,o.name DESC LIMIT %(limit)s OFFSET %(start)s", values, as_dict=True)
        campuses = frappe.get_all("Campus", filters={"name": ["in", scope]} if scope is not None else {}, pluck="name", order_by="name", limit_page_length=0)
        return {"items": [order_payload(frappe.get_doc(ORDER_TYPE, r.name)) for r in rows[:80]], "has_more": len(rows) > 80, "campuses": campuses, "can_issue": scope is None}
    if action == "create":
        from qas_custom.services.material_orders import _create_store_order, _get_parent
        _family_access(data.get("parent"), scope)
        if scope is not None and data.get("pickup_campus") not in scope:
            frappe.throw(_("Choose an assigned campus."), frappe.PermissionError)
        return _create_store_order(_get_parent(data.get("parent")), data, admin=True)
    if not data.get("order"):
        frappe.throw(_("Choose an order."))
    doc = _lock(data.get("order"), scope)
    if action == "get":
        return order_payload(doc)
    if action in {"preview", "edit"}:
        if doc.status == "Cancelled" or doc.get("pending_revision"):
            frappe.throw(_("This order is cancelled or already awaiting approval."))
        changes = _changes(doc, data, scope)
        if action == "preview":
            return _preview(doc, changes)
        _confirm(doc, changes, data.get("confirmation"))
        if scope is not None and financials(doc)["invoice_status"] != "Draft":
            doc.pending_revision = json.dumps(changes)
            doc.save(ignore_permissions=True)
            doc.add_comment("Comment", f"Revision requested by {frappe.session.user}: {changes['reason']}")
        else:
            _revise(doc, changes)
    elif action in {"preview_approval", "approve"}:
        if scope is not None:
            frappe.throw(_("School Admin approval is required."), frappe.PermissionError)
        if doc.status == "Cancelled":
            frappe.throw(_("Cancelled orders cannot be issued."))
        if doc.get("pending_revision"):
            changes = json.loads(doc.pending_revision)
            if action == "preview_approval":
                return _preview(doc, changes)
            _confirm(doc, changes, data.get("confirmation"))
            if changes.get("cancel"):
                _cancel(doc, changes["reason"])
            else:
                _revise(doc, changes)
                doc.pending_revision = None
                doc.save(ignore_permissions=True)
                if doc.invoice:
                    approved = frappe.get_doc("Sales Invoice", doc.invoice)
                    if cint(approved.docstatus) == 0:
                        _mutation(approved.submit)
                        _notify(doc, approved)
        elif action == "approve":
            _issue_draft(doc)
    elif action == "reject_revision":
        if scope is not None:
            frappe.throw(_("School Admin approval is required."), frappe.PermissionError)
        if not str(data.get("reason") or "").strip():
            frappe.throw(_("Enter a reason for rejecting this revision."))
        doc.pending_revision = None
        doc.save(ignore_permissions=True)
        doc.add_comment("Comment", f"Revision rejected by {frappe.session.user}: {data['reason']}")
    elif action in {"preview_cancel", "cancel"}:
        if doc.status not in {"Ordered", "Ready for collection"} or doc.get("pending_revision"):
            frappe.throw(_("Only un-dispatched orders without a pending revision can be cancelled."))
        reason = str(data.get("reason") or "").strip()
        if not reason:
            frappe.throw(_("Enter a cancellation reason."))
        changes = {"cancel": True, "reason": reason, "items": [r.as_dict() for r in doc.get("items") or []],
            "discount_type": "Percent", "discount_value": 100, "discount_reason": reason}
        if action == "preview_cancel":
            return _preview(doc, changes)
        _confirm(doc, changes, data.get("confirmation"))
        if scope is not None:
            doc.pending_revision = json.dumps(changes)
            doc.save(ignore_permissions=True)
        else:
            _cancel(doc, reason)
    elif action == "status":
        target = data.get("status")
        if target == "Dispatched" and (doc.get("pending_revision") or financials(doc)["invoice_status"] == "Draft" and doc.invoice):
            frappe.throw(_("School Admin must issue or approve this invoice before dispatch. Payment can follow later."))
        allowed = {"Ordered": {"Ready for collection", "Dispatched"}, "Ready for collection": {"Dispatched"}}
        if target not in allowed.get(doc.status, set()):
            frappe.throw(_("This status change is not allowed."))
        prefix = "ready" if target == "Ready for collection" else "collected"
        doc.status = target if target != "Dispatched" else "Collected"
        doc.set(prefix + "_at", now_datetime()); doc.set(prefix + "_by", frappe.session.user)
        doc.save(ignore_permissions=True)
        doc.add_comment("Comment", f"{target} by {frappe.session.user}")
        if target == "Ready for collection":
            from qas_custom.modules.notifications.store_order_notifications import queue_ready_notification
            queue_ready_notification(doc)
    elif action == "pay":
        from qas_custom.services.school_admin import _create_payment_entry_for_invoice
        before = financials(doc)
        amount = money(data.get("amount"))
        if doc.get("pending_revision") or before["invoice_status"] != "Issued" or amount <= 0 or amount > before["remaining_amount"]:
            frappe.throw(_("Refresh this order and enter an amount up to its remaining balance. Draft or cancelled invoices cannot be paid."))
        if str(doc.modified) != str(data.get("modified")) or money(data.get("expected_paid")) != before["paid_amount"]:
            frappe.throw(_("The order or payments changed. Refresh before recording payment."))
        method = str(data.get("mode_of_payment") or "").strip()
        if not method or not frappe.db.exists("Mode of Payment", {"name": method, "enabled": 1}) or "credit" in method.lower():
            frappe.throw(_("Choose a valid payment method. Store Credit is not accepted."))
        remaining = amount
        for row in before["invoices"]:
            if cint(row.docstatus) == 1 and not cint(row.is_return) and flt(row.outstanding_amount) > 0:
                part = min(remaining, flt(row.outstanding_amount))
                invoice = frappe.get_doc("Sales Invoice", row.name)
                _create_payment_entry_for_invoice(invoice, amount=part, mode_of_payment=method, reference_no=data.get("reference_no"))
                remaining = money(remaining - part)
                if remaining <= 0:
                    break
        if remaining > 0:
            frappe.throw(_("Invoice balances changed. No payment was saved; refresh and try again."))
        doc.add_comment("Comment", f"Payment {amount:.2f} recorded by {frappe.session.user}")
        doc.save(ignore_permissions=True)
        _notify(doc)
    elif action == "retry_ready":
        if doc.status != "Ready for collection":
            frappe.throw(_("This order is not ready for collection."))
        from qas_custom.modules.notifications.store_order_notifications import queue_ready_notification
        queue_ready_notification(doc, retry=True)
    elif action == "resend_invoice":
        if not doc.invoice or financials(doc)["invoice_status"] == "Draft":
            frappe.throw(_("Issue the invoice before sending it."))
        _notify(doc, frappe.get_doc("Sales Invoice", doc.invoice))
    else:
        frappe.throw(_("Unknown order action."))
    return order_payload(doc)


@frappe.whitelist()
def parent_invoices():
    from qas_custom.services.material_orders import _require_parent_shop
    parent = _require_parent_shop()
    rows = frappe.get_all(ORDER_TYPE, filters={"parent": parent.name}, pluck="name", order_by="modified desc", limit_page_length=0)
    return {"items": [order_payload(frappe.get_doc(ORDER_TYPE, name), parent=True) for name in rows]}


@frappe.whitelist()
def download_invoice(invoice=None):
    """Use the existing PDF renderer with order ownership/campus checks."""
    from qas_custom.services.material_orders import _require_parent_shop
    from qas_custom.modules.notifications.commands import render_parent_invoice_pdf
    doc = frappe.get_doc("Sales Invoice", invoice)
    if not is_order_invoice(doc) or doc.get("source_doctype") != ORDER_TYPE or not doc.get("source_document"):
        frappe.throw(_("This is not a linked Order Invoice."), frappe.PermissionError)
    order = frappe.get_doc(ORDER_TYPE, doc.source_document)
    privileged = {"School Admin", "System Manager"}.intersection(frappe.get_roles()) or frappe.db.exists("Campus Admin Profile", {"user": frappe.session.user, "active": 1})
    if privileged:
        _access(order, _scope())
    else:
        parent = _require_parent_shop()
        if order.parent != parent.name or cint(doc.docstatus) == 0:
            frappe.throw(_("This issued invoice is not available to your family."), frappe.PermissionError)
    frappe.local.response.filename = f"{doc.name}.pdf"
    frappe.local.response.filecontent = render_parent_invoice_pdf(doc.name, store_credit_applied=0)
    frappe.local.response.type = "download"
