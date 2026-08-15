from __future__ import annotations

import imghdr
import json
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime, nowdate
from frappe.utils.file_manager import save_file


PRODUCT_DOCTYPE = "Store Product"
ORDER_DOCTYPE = "Store Order"
ADMIN_ROLES = {"School Admin", "System Manager"}
IMAGE_FORMATS = {"jpeg": "jpg", "png": "png", "webp": "webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def get_school_admin_store_products_data(active=None, query=None, limit=160):
	_require_school_admin()
	filters = {}
	if active is not None and str(active) != "":
		filters["active"] = cint(active)
	if query:
		filters["product_name"] = ["like", f"%{str(query).strip()}%"]
	rows = frappe.get_all(
		PRODUCT_DOCTYPE,
		filters=filters,
		fields=["name", "product_name", "description", "unit_price", "active", "display_order", "item_code", "modified"],
		order_by="display_order asc, product_name asc, modified desc",
		limit_page_length=_limit(limit, 160, 400),
	)
	return {"items": [_product_payload(frappe.get_doc(PRODUCT_DOCTYPE, row.name), include_media=False) for row in rows]}


def get_school_admin_store_product_data(product=None):
	_require_school_admin()
	return _product_payload(_get_product(product), include_media=True)


def save_school_admin_store_product_data(product=None, payload=None):
	_require_school_admin()
	data = _payload(payload)
	doc = _get_product(product) if product else frappe.new_doc(PRODUCT_DOCTYPE)
	doc.product_name = str(data.get("product_name") or "").strip()
	doc.description = str(data.get("description") or "").strip()
	doc.unit_price = flt(data.get("unit_price"))
	doc.active = cint(data.get("active", 1))
	doc.display_order = cint(data.get("display_order") or 0)
	if not doc.product_name:
		frappe.throw(_("Product name is required."))
	if doc.unit_price < 0:
		frappe.throw(_("Product price cannot be negative."))
	_apply_media(doc, data)
	doc.save(ignore_permissions=True)
	doc.item_code = _ensure_material_item(doc)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _product_payload(doc, include_media=True)


def upload_school_admin_store_product_image_data(product=None):
	_require_school_admin()
	doc = _get_product(product)
	upload = frappe.request.files.get("image") if frappe.request else None
	if not upload:
		frappe.throw(_("Choose an image to upload."))
	content = upload.read()
	if not content or len(content) > MAX_IMAGE_BYTES:
		frappe.throw(_("Images must be 8 MB or smaller."))
	image_type = imghdr.what(None, h=content)
	if image_type not in IMAGE_FORMATS:
		frappe.throw(_("Use a JPG, PNG, or WEBP image."))
	base_name = (upload.filename or "material-product").rsplit(".", 1)[0]
	file_doc = save_file(
		f"{base_name}.{IMAGE_FORMATS[image_type]}", content, PRODUCT_DOCTYPE, doc.name, is_private=0, df="images"
	)
	doc.append("images", {"image": file_doc.file_url, "display_order": len(doc.get("images") or [])})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _product_payload(doc, include_media=True)


def get_school_admin_store_orders_data(status=None, query=None, limit=160):
	_require_school_admin()
	filters = {}
	if status:
		filters["status"] = status
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters=filters,
		fields=["name", "parent", "customer", "status", "invoice", "pickup_campus", "pickup_date", "pickup_time", "modified"],
		order_by="modified desc",
		limit_page_length=_limit(limit, 160, 400),
	)
	items = []
	needle = str(query or "").strip().lower()
	for row in rows:
		payload = _order_payload(frappe.get_doc(ORDER_DOCTYPE, row.name), include_items=True)
		if needle and needle not in _order_search_text(payload):
			continue
		items.append(payload)
	return {"items": items}


def get_school_admin_store_order_data(order=None):
	_require_school_admin()
	return _order_payload(_get_order(order), include_items=True)


def get_school_admin_store_order_options_data(parent=None):
	_require_school_admin()
	parent_doc = _get_parent(parent)
	return {"parent": _parent_payload(parent_doc), "pickup_sessions": _pickup_sessions_for_parent(parent_doc.name)}


def create_school_admin_store_order_data(payload=None):
	_require_school_admin()
	data = _payload(payload)
	parent_doc = _get_parent(data.get("parent"))
	customer = str(parent_doc.get("customer") or "").strip()
	if not customer:
		frappe.throw(_("This parent does not have a customer account. Open the family record and save it first."))
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("The parent customer account was not found."))
	pickup = _validate_pickup_session(parent_doc.name, data.get("pickup_course_session"))
	items = _store_order_items(data.get("items"))
	order = frappe.get_doc(
		{
		"doctype": ORDER_DOCTYPE,
			"parent": parent_doc.name,
			"customer": customer,
			"status": "Ordered",
			"pickup_course_session": pickup["name"],
			"pickup_campus": pickup.get("campus"),
			"pickup_date": pickup.get("session_date"),
			"pickup_time": pickup.get("start_time") or "",
			"items": items,
		}
	)
	order.insert(ignore_permissions=True)
	invoice = _create_and_submit_material_invoice(order, parent_doc)
	order.invoice = invoice.name
	order.save(ignore_permissions=True)
	order.add_comment("Comment", _("Store order created by {0}. Invoice: {1}.").format(frappe.session.user, invoice.name))
	frappe.db.commit()
	return _order_payload(order, include_items=True)


def update_school_admin_store_order_status_data(order=None, status=None, reason=None):
	_require_school_admin()
	doc = _get_order(order)
	status = str(status or "").strip()
	allowed = {
		"Ordered": {"Ready for collection", "Cancelled"},
		"Ready for collection": {"Collected", "Cancelled"},
	}
	if status not in allowed.get(doc.status, set()):
		frappe.throw(_("This order cannot be changed from {0} to {1}.").format(doc.status, status))
	if status == "Cancelled":
		_cancel_order(doc, reason)
	else:
		doc.status = status
		if status == "Ready for collection":
			doc.ready_at = now_datetime()
			doc.ready_by = frappe.session.user
		elif status == "Collected":
			doc.collected_at = now_datetime()
			doc.collected_by = frappe.session.user
		doc.save(ignore_permissions=True)
		doc.add_comment("Comment", _("Store order marked {0} by {1}.").format(status, frappe.session.user))
		frappe.db.commit()
	return _order_payload(doc, include_items=True)


def _cancel_order(doc, reason=None):
	reason = str(reason or "").strip()
	invoice = frappe.get_doc("Sales Invoice", doc.invoice) if doc.invoice else None
	if invoice and cint(invoice.docstatus) == 1 and flt(invoice.outstanding_amount) > 0.005:
		from qas_custom.services.school_admin import cancel_school_admin_invoice_data

		cancel_school_admin_invoice_data(
			invoice=invoice.name,
		reason=reason or "Store order cancelled",
			allow_empty_reason=True,
			send_notifications=True,
		)
	doc.status = "Cancelled"
	doc.cancelled_at = now_datetime()
	doc.cancelled_by = frappe.session.user
	doc.cancellation_reason = reason
	doc.save(ignore_permissions=True)
	doc.add_comment(
		"Comment",
		_("Store order cancelled by {0}.{1}").format(frappe.session.user, f" Reason: {reason}" if reason else ""),
	)
	frappe.db.commit()


def _create_and_submit_material_invoice(order, parent_doc):
	from qas_custom.services.school_admin import submit_school_admin_invoice_data

	invoice = frappe.new_doc("Sales Invoice")
	_set_if_field(invoice, "customer", order.customer)
	_set_if_field(invoice, "due_date", nowdate())
	_set_if_field(invoice, "parent", order.parent)
	_set_if_field(invoice, "qas_invoice_type", "Store Order")
	_set_if_field(invoice, "source_type", "Store Order")
	_set_if_field(invoice, "source_doctype", ORDER_DOCTYPE)
	_set_if_field(invoice, "source_document", order.name)
	_set_if_field(invoice, "qas_is_manual_invoice", 0)
	_set_if_field(invoice, "qas_apply_store_credit_on_submit", 0)
	_set_if_field(invoice, "remarks", _("Store order {0}. Pickup: {1} {2} at {3}.").format(order.name, order.pickup_campus or "", order.pickup_date or "", order.pickup_time or ""))
	for row in order.get("items") or []:
		invoice.append(
			"items",
			{
				"item_code": row.item_code,
				"item_name": row.product_name,
				"description": row.product_name,
				"qty": cint(row.qty),
				"rate": flt(row.unit_price),
			},
		)
	invoice.insert(ignore_permissions=True)
	frappe.db.commit()
	submit_school_admin_invoice_data(invoice=invoice.name, enqueue_notification=True, send_notifications=True)
	return frappe.get_doc("Sales Invoice", invoice.name)


def _store_order_items(rows):
	if not isinstance(rows, list) or not rows:
		frappe.throw(_("Add at least one material product."))
	items, seen = [], set()
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		product_name = str(raw.get("store_product") or raw.get("product") or "").strip()
		qty = cint(raw.get("qty"))
		if not product_name or qty <= 0:
			frappe.throw(_("Each material order line needs an active product and quantity greater than zero."))
		if product_name in seen:
			frappe.throw(_("Add each material product once, then set its quantity."))
		product = _get_product(product_name)
		if not cint(product.active):
			frappe.throw(_("Inactive product cannot be ordered: {0}.").format(product.product_name))
		item_code = _ensure_material_item(product)
		product.item_code = item_code
		product.save(ignore_permissions=True)
		seen.add(product_name)
		items.append(
			{
				"store_product": product.name,
				"product_name": product.product_name,
				"item_code": item_code,
				"unit_price": flt(product.unit_price),
				"qty": qty,
				"amount": flt(product.unit_price) * qty,
			}
		)
	if not items:
		frappe.throw(_("Add at least one material product."))
	return items


def _pickup_sessions_for_parent(parent):
	timeslots = frappe.get_all(
		"Enrollment",
		filters={"parent": parent, "status": ["in", ["Planned", "Active"]], "weekly_timeslot": ["is", "set"]},
		pluck="weekly_timeslot",
		limit_page_length=0,
	)
	if not timeslots:
		return []
	session_filters = {"weekly_timeslot": ["in", sorted(set(timeslots))], "session_date": [">=", nowdate()]}
	if _has_field("Course Sessions", "status"):
		session_filters["status"] = ["!=", "Cancelled"]
	sessions = frappe.get_all(
		"Course Sessions",
		filters=session_filters,
		fields=_safe_fields("Course Sessions", ["name", "weekly_timeslot", "session_date", "status"]),
		order_by="session_date asc",
		limit_page_length=0,
	)
	if not sessions:
		return []
	timeslot_data = {
		row.name: row
		for row in frappe.get_all(
			"Weekly Timeslot",
			filters={"name": ["in", list({row.weekly_timeslot for row in sessions})]},
			fields=_safe_fields("Weekly Timeslot", ["name", "campus", "start_time", "course"]),
			limit_page_length=0,
		)
	}
	return [
		{
			"name": row.name,
			"session_date": row.session_date,
			"status": row.get("status"),
			"weekly_timeslot": row.weekly_timeslot,
			"campus": timeslot_data.get(row.weekly_timeslot, {}).get("campus"),
			"start_time": str(timeslot_data.get(row.weekly_timeslot, {}).get("start_time") or ""),
			"course": timeslot_data.get(row.weekly_timeslot, {}).get("course"),
		}
		for row in sessions
	]


def _validate_pickup_session(parent, course_session):
	if not course_session:
		frappe.throw(_("Choose a future class session for pickup."))
	for row in _pickup_sessions_for_parent(parent):
		if row["name"] == course_session:
			return row
	frappe.throw(_("Pickup session must be a future class session for this family."))


def _ensure_material_item(product):
	if product.get("item_code") and frappe.db.exists("Item", product.item_code):
		item = frappe.get_doc("Item", product.item_code)
		_set_if_field(item, "item_name", product.product_name)
		_set_if_field(item, "disabled", 0 if cint(product.active) else 1)
		item.save(ignore_permissions=True)
		return item.name
	from qas_custom.services.school_admin import _default_school_admin_item_group, _default_school_admin_stock_uom

	item = frappe.new_doc("Item")
	item_code = f"QAS-MATERIAL-{product.name}"
	_set_if_field(item, "item_code", item_code)
	_set_if_field(item, "item_name", product.product_name)
	_set_if_field(item, "item_group", _default_school_admin_item_group())
	_set_if_field(item, "stock_uom", _default_school_admin_stock_uom())
	_set_if_field(item, "is_stock_item", 0)
	_set_if_field(item, "disabled", 0 if cint(product.active) else 1)
	item.insert(ignore_permissions=True)
	return item.name


def _product_payload(doc, include_media=True):
	payload = {
		"name": doc.name,
		"product_name": doc.product_name or "",
		"description": doc.description or "",
		"unit_price": flt(doc.unit_price),
		"active": cint(doc.active),
		"display_order": cint(doc.display_order),
		"item_code": doc.item_code or "",
		"modified": doc.modified,
	}
	if include_media:
		payload["images"] = [
			{"image": row.image, "display_order": cint(row.display_order or index)}
			for index, row in enumerate(doc.get("images") or [])
			if row.image
		]
		payload["videos"] = [
			{"label": row.label, "url": row.url, "display_order": cint(row.display_order or index)}
			for index, row in enumerate(doc.get("videos") or [])
		]
	return payload


def _order_payload(doc, include_items=True):
	invoice = _invoice_payment_payload(doc.invoice)
	payload = {
		"name": doc.name,
		"parent": doc.parent,
		"parent_name": frappe.db.get_value("Parent", doc.parent, "parent_name") or doc.parent,
		"customer": doc.customer,
		"status": doc.status,
		"invoice": doc.invoice,
		"invoice_payment_status": invoice["status"],
		"invoice_outstanding_amount": invoice["outstanding_amount"],
		"invoice_total": invoice["total"],
		"pickup_course_session": doc.pickup_course_session,
		"pickup_campus": doc.pickup_campus,
		"pickup_date": doc.pickup_date,
		"pickup_time": doc.pickup_time,
		"ready_at": doc.ready_at,
		"collected_at": doc.collected_at,
		"cancelled_at": doc.cancelled_at,
		"cancellation_reason": doc.cancellation_reason,
		"modified": doc.modified,
	}
	if include_items:
		payload["items"] = [
			{
				"store_product": row.store_product,
				"product_name": row.product_name,
				"item_code": row.item_code,
				"unit_price": flt(row.unit_price),
				"qty": cint(row.qty),
				"amount": flt(row.amount),
			}
			for row in doc.get("items") or []
		]
	return payload


def _invoice_payment_payload(invoice_name):
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return {"status": "Not created", "outstanding_amount": 0, "total": 0}
	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if cint(invoice.docstatus) == 2:
		return {"status": "Cancelled", "outstanding_amount": 0, "total": flt(invoice.grand_total)}
	if cint(invoice.docstatus) == 0:
		return {"status": "Draft", "outstanding_amount": flt(invoice.grand_total), "total": flt(invoice.grand_total)}
	return {
		"status": "Paid" if flt(invoice.outstanding_amount) <= 0.005 else "Unpaid",
		"outstanding_amount": max(0, flt(invoice.outstanding_amount)),
		"total": flt(invoice.grand_total),
	}


def _apply_media(doc, data):
	if "images" in data:
		doc.images = []
		for index, row in enumerate(data.get("images") or []):
			image = str((row or {}).get("image") or "").strip() if isinstance(row, dict) else ""
			if image:
				doc.append("images", {"image": image, "display_order": cint((row or {}).get("display_order") or index)})
	if "videos" in data:
		doc.videos = []
		for index, row in enumerate(data.get("videos") or []):
			if not isinstance(row, dict):
				continue
			label, url = str(row.get("label") or "").strip(), str(row.get("url") or "").strip()
			if not label and not url:
				continue
			if not label or not _safe_url(url):
				frappe.throw(_("Each external video needs a label and a valid http or https URL."))
			doc.append("videos", {"label": label, "url": url, "display_order": cint(row.get("display_order") or index)})


def _safe_url(value):
	parsed = urlparse(value)
	return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _get_product(product):
	if not product:
		frappe.throw(_("Material product is required."))
	return frappe.get_doc(PRODUCT_DOCTYPE, product)


def _get_order(order):
	if not order:
		frappe.throw(_("Material order is required."))
	return frappe.get_doc(ORDER_DOCTYPE, order)


def _get_parent(parent):
	if not parent:
		frappe.throw(_("Choose a family."))
	return frappe.get_doc("Parent", parent)


def _parent_payload(doc):
	return {"name": doc.name, "parent_name": doc.parent_name or doc.name, "customer": doc.customer or ""}


def _order_search_text(payload):
	return " ".join(
		[
			str(payload.get("name") or ""),
			str(payload.get("parent_name") or ""),
			str(payload.get("invoice") or ""),
			*(str(row.get("product_name") or "") for row in payload.get("items") or []),
		]
	).lower()


def _payload(value):
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("Material order payload is invalid."))
	if not isinstance(value, dict):
		frappe.throw(_("Material order payload is invalid."))
	return value


def _require_school_admin():
	if not ADMIN_ROLES.intersection(set(frappe.get_roles(frappe.session.user))):
		frappe.throw(_("School Admin access is required."), frappe.PermissionError)


def _has_field(doctype, fieldname):
	return frappe.db.exists("DocField", {"parent": doctype, "fieldname": fieldname})


def _safe_fields(doctype, names):
	return [name for name in names if name == "name" or _has_field(doctype, name)]


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _limit(value, default, maximum):
	return max(1, min(cint(value or default), maximum))
