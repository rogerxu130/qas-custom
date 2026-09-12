from __future__ import annotations

import imghdr
import json
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime
from frappe.utils.file_manager import save_file


PRODUCT_DOCTYPE = "Store Product"
PRODUCT_CATEGORY_DOCTYPE = "Store Product Category"
ORDER_DOCTYPE = "Store Order"
ADMIN_ROLES = {"School Admin", "System Manager"}
IMAGE_FORMATS = {"jpeg": "jpg", "png": "png", "webp": "webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024


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


def get_school_admin_store_product_categories_data(active=None, query=None, limit=160):
	_require_school_admin()
	filters = {}
	if active is not None and str(active) != "":
		filters["active"] = cint(active)
	if query:
		filters["category_name"] = ["like", f"%{str(query).strip()}%"]
	rows = frappe.get_all(
		PRODUCT_CATEGORY_DOCTYPE,
		filters=filters,
		fields=["name", "category_name", "display_order", "active", "modified"],
		order_by="display_order asc, category_name asc, modified desc",
		limit_page_length=_limit(limit, 160, 400),
	)
	return {"items": [_category_payload(row) for row in rows]}


def save_school_admin_store_product_category_data(category=None, payload=None):
	_require_school_admin()
	data = _payload(payload)
	doc = frappe.get_doc(PRODUCT_CATEGORY_DOCTYPE, category) if category else frappe.new_doc(PRODUCT_CATEGORY_DOCTYPE)
	doc.category_name = str(data.get("category_name") or "").strip()
	doc.display_order = cint(data.get("display_order") or 0)
	doc.active = cint(data.get("active", 1))
	if not doc.category_name:
		frappe.throw(_("Category name is required."))
	duplicate = frappe.db.exists(
		PRODUCT_CATEGORY_DOCTYPE,
		{"category_name": doc.category_name, "name": ["!=", doc.name or ""]},
	)
	if duplicate:
		frappe.throw(_("A product category with this name already exists."))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _category_payload(doc)


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
	_apply_product_categories(doc, data)
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
	next_order = max((_media_display_order(row, index) for index, row in enumerate(doc.get("images") or [])), default=-1) + 1
	doc.append("images", {"image": file_doc.file_url, "display_order": next_order})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _product_payload(doc, include_media=True)


def upload_school_admin_store_product_video_data(product=None):
	_require_school_admin()
	doc = _get_product(product)
	upload = frappe.request.files.get("video") if frappe.request else None
	if not upload:
		frappe.throw(_("Choose an MP4 video to upload."))
	content = upload.read(MAX_VIDEO_BYTES + 1)
	_validate_product_video(upload.filename, content)
	file_doc = save_file(
		"product-video.mp4", content, PRODUCT_DOCTYPE, doc.name, is_private=0, df="videos"
	)
	next_order = max((_media_display_order(row, index) for index, row in enumerate(doc.get("videos") or [])), default=-1) + 1
	doc.append("videos", {"label": f"{doc.product_name} video", "url": file_doc.file_url, "display_order": next_order})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return _product_payload(doc, include_media=True)


def delete_school_admin_store_product_video_data(product=None, url=None, modified=None):
	_require_school_admin()
	url = str(url or "").strip()
	if not _is_uploaded_product_video(url):
		frappe.throw(_("Only uploaded product MP4 files can be deleted here."))
	frappe.db.get_value(PRODUCT_DOCTYPE, product, "name", for_update=True)
	doc = _get_product(product)
	if not modified or str(doc.modified) != str(modified):
		frappe.throw(_("The product changed. Refresh it before deleting a video."))
	if not any(row.url == url for row in doc.get("videos") or []):
		frappe.throw(_("This video is no longer listed on the product. Refresh it first."))
	files = frappe.get_all("File", filters={"file_url": url}, fields=["name", "attached_to_doctype", "attached_to_name", "is_private"])
	if len(files) != 1 or files[0].attached_to_doctype != PRODUCT_DOCTYPE or files[0].attached_to_name != doc.name or cint(files[0].is_private):
		frappe.throw(_("This file is missing or shared with another attachment. It cannot be deleted here."))
	if frappe.db.exists("Store Product Video", {"url": url, "parent": ["!=", doc.name]}):
		frappe.throw(_("Another product uses this video. Remove its reference before deleting the file."))
	doc.set("videos", [row for row in doc.get("videos") or [] if row.url != url])
	doc.save(ignore_permissions=True)
	# Use Frappe's File lifecycle for physical storage deletion and attachment audit.
	frappe.delete_doc("File", files[0].name, ignore_permissions=True)
	frappe.db.commit()
	return _product_payload(doc, include_media=True)


def _validate_product_video(filename, content):
	if not content or len(content) > MAX_VIDEO_BYTES:
		frappe.throw(_("Videos must be non-empty and 50 MB or smaller."))
	# ISO BMFF file-type box, with an MP4-compatible major or compatible brand.
	box_size = int.from_bytes(content[:4], "big")
	brands = [content[8:12]] + [content[i:i + 4] for i in range(16, min(box_size, len(content)), 4)]
	if (not str(filename or "").lower().endswith(".mp4") or len(content) < 24
		or content[4:8] != b"ftyp" or box_size < 16 or box_size > min(len(content), 4096)
		or not set(brands).intersection({b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V "})):
		frappe.throw(_("Use an MP4 video. For browser playback, export with H.264 video and AAC audio."))


def _is_uploaded_product_video(url):
	return url.startswith("/files/") and url.lower().endswith(".mp4") and ".." not in url and "?" not in url and "#" not in url


def get_school_admin_store_orders_data(status=None, query=None, limit=160, campus=None, start=0):
	_require_school_admin()
	conditions, values = [], {"limit": _limit(limit, 160, 400) + 1, "start": max(0, cint(start))}
	if status:
		conditions.append("o.status = %(status)s")
		values["status"] = status
	if campus:
		conditions.append("o.pickup_campus = %(campus)s")
		values["campus"] = campus
	if query and str(query).strip():
		values["query"] = "%" + str(query).strip() + "%"
		conditions.append("""(o.name LIKE %(query)s OR o.parent LIKE %(query)s
			OR EXISTS (SELECT 1 FROM `tabParent` p WHERE p.name=o.parent AND p.parent_name LIKE %(query)s)
			OR EXISTS (SELECT 1 FROM `tabStore Order Item` i WHERE i.parent=o.name AND i.parenttype='Store Order' AND i.product_name LIKE %(query)s))""")
	where = " AND ".join(conditions) or "1=1"
	rows = frappe.db.sql(
		f"SELECT o.name FROM `tabStore Order` o WHERE {where} ORDER BY o.modified DESC, o.name DESC LIMIT %(limit)s OFFSET %(start)s",
		values, as_dict=True,
	)
	page_size = values["limit"] - 1
	return {
		"items": [_order_payload(frappe.get_doc(ORDER_DOCTYPE, row.name), include_items=True) for row in rows[:page_size]],
		"has_more": len(rows) > page_size,
		"campuses": frappe.get_all("Campus", pluck="name", order_by="name asc", limit_page_length=0),
	}


def get_school_admin_store_order_data(order=None):
	_require_school_admin()
	return _order_payload(_get_order(order), include_items=True)


def get_school_admin_store_order_options_data(parent=None):
	_require_school_admin()
	parent_doc = _get_parent(parent)
	return _store_order_options(parent_doc, admin=True)


def create_school_admin_store_order_data(payload=None):
	_require_school_admin()
	data = _payload(payload)
	parent_doc = _get_parent(data.get("parent"))
	return _create_store_order(parent_doc, data, admin=True)


def get_parent_store_products_data(limit=80):
	parent_doc = _require_parent_shop_testing()
	rows = frappe.get_all(
		PRODUCT_DOCTYPE,
		filters={"active": 1},
		fields=["name"],
		order_by="display_order asc, product_name asc, modified desc",
		limit_page_length=_limit(limit, 80, 160),
	)
	categories = frappe.get_all(
		PRODUCT_CATEGORY_DOCTYPE,
		filters={"active": 1},
		fields=["name", "category_name", "display_order", "active", "modified"],
		order_by="display_order asc, category_name asc, modified desc",
		limit_page_length=0,
	)
	return {
		"items": [_parent_product_payload(frappe.get_doc(PRODUCT_DOCTYPE, row.name)) for row in rows],
		"categories": [_category_payload(row) for row in categories],
		"parent": _parent_payload(parent_doc),
	}


def get_parent_store_order_options_data():
	parent_doc = _require_parent_shop_testing()
	return _store_order_options(parent_doc)


def get_parent_store_orders_data(limit=80):
	parent_doc = _require_parent_shop_testing()
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"parent": parent_doc.name},
		fields=["name"],
		order_by="modified desc",
		limit_page_length=_limit(limit, 80, 160),
	)
	return {"items": [_order_payload(frappe.get_doc(ORDER_DOCTYPE, row.name), include_items=True) for row in rows]}


def get_parent_store_order_data(order=None):
	parent_doc = _require_parent_shop_testing()
	doc = _get_order(order)
	if doc.parent != parent_doc.name:
		frappe.throw(_("This order does not belong to your family."), frappe.PermissionError)
	return _order_payload(doc, include_items=True)


def create_parent_store_order_data(payload=None):
	parent_doc = _require_parent_shop_testing()
	return _create_store_order(parent_doc, _payload(payload))


def _create_store_order(parent_doc, data, *, admin=False):
	options = _store_order_options(parent_doc, admin=admin)
	campus = str(data.get("pickup_campus") or "").strip()
	if not campus or campus not in {row["name"] for row in options["pickup_campuses"]}:
		frappe.throw(_("Choose an available pickup campus."))
	order = frappe.get_doc({
		"doctype": ORDER_DOCTYPE,
		"parent": parent_doc.name,
		"status": "Ordered",
		"pickup_campus": campus,
		"items": _store_order_items(data.get("items")),
	})
	order.insert(ignore_permissions=True)
	order.add_comment("Comment", _("Store order created by {0}.").format(frappe.session.user))
	return _order_payload(order, include_items=True)


def _locked_order(order):
	if not order:
		frappe.throw(_("Choose an order."))
	# Serialize transitions and notification retries so simultaneous clicks cannot duplicate mail.
	frappe.db.sql("SELECT name FROM `tabStore Order` WHERE name=%s FOR UPDATE", (order,))
	return _get_order(order)


def update_school_admin_store_order_status_data(order=None, status=None, reason=None):
	_require_school_admin()
	doc = _locked_order(order)
	status = str(status or "").strip()
	allowed = {
		"Ordered": {"Ready for collection", "Collected", "Cancelled"},
		"Ready for collection": {"Collected", "Cancelled"},
	}
	if status not in allowed.get(doc.status, set()):
		frappe.throw(_("This order cannot be changed from {0} to {1}.").format(doc.status, status))
	doc.status = status
	prefix = {"Ready for collection": "ready", "Collected": "collected", "Cancelled": "cancelled"}[status]
	setattr(doc, prefix + "_at", now_datetime())
	setattr(doc, prefix + "_by", frappe.session.user)
	if status == "Cancelled":
		doc.cancellation_reason = str(reason or "").strip()
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", _("Store order marked {0} by {1}.").format(status, frappe.session.user))
	if status == "Ready for collection":
		from qas_custom.modules.notifications.store_order_notifications import queue_ready_notification
		queue_ready_notification(doc)
	return _order_payload(doc, include_items=True)


def retry_school_admin_store_order_notification_data(order=None):
	_require_school_admin()
	doc = _locked_order(order)
	if doc.status != "Ready for collection":
		frappe.throw(_("Only orders ready for collection can send a pickup notification."))
	from qas_custom.modules.notifications.store_order_notifications import queue_ready_notification
	queue_ready_notification(doc, retry=True)
	return _order_payload(doc, include_items=True)


def _store_order_options(parent_doc, admin=False):
	campuses = frappe.get_all("Campus", filters={"status": "Active"}, fields=["name", "address", "phone"], order_by="name asc", limit_page_length=0)
	timeslots = frappe.get_all("Enrollment", filters={"parent": parent_doc.name, "status": ["in", ["Planned", "Active"]], "weekly_timeslot": ["is", "set"]}, pluck="weekly_timeslot", limit_page_length=0)
	enrolled = set(frappe.get_all("Weekly Timeslot", filters={"name": ["in", timeslots]}, pluck="campus", limit_page_length=0)) if timeslots else set()
	family_campuses = [row for row in campuses if row.name in enrolled]
	choices = campuses if admin or not family_campuses else family_campuses
	default = family_campuses[0].name if len(family_campuses) == 1 else (choices[0].name if len(choices) == 1 else "")
	return {"parent": _parent_payload(parent_doc), "pickup_campuses": choices, "default_campus": default}


def _store_order_items(rows):
	if not isinstance(rows, list) or not rows:
		frappe.throw(_("Add at least one material product."))
	items, seen = [], set()
	for raw in rows:
		if not isinstance(raw, dict):
			continue
		product_name = str(raw.get("store_product") or raw.get("product") or "").strip()
		quantity = flt(raw.get("qty"))
		qty = cint(quantity)
		if quantity != qty or qty > 9999:
			frappe.throw(_("Quantity must be a whole number between 1 and 9999."))
		if not product_name or qty <= 0:
			frappe.throw(_("Each material order line needs an active product and quantity greater than zero."))
		if product_name in seen:
			frappe.throw(_("Add each material product once, then set its quantity."))
		product = _get_product(product_name)
		if not cint(product.active):
			frappe.throw(_("Inactive product cannot be ordered: {0}.").format(product.product_name))
		item_code = product.get("item_code")
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
	category_ids = _product_category_names(doc)
	categories = [category for name in category_ids if (category := _category_payload_for_product(name))]
	category = categories[0] if categories else None
	payload = {
		"name": doc.name,
		"product_name": doc.product_name or "",
		"description": doc.description or "",
		"unit_price": flt(doc.unit_price),
		"active": cint(doc.active),
		"display_order": cint(doc.display_order),
		"item_code": doc.item_code or "",
		"primary_category": doc.get("primary_category") or "",
		"category": category,
		"category_ids": category_ids,
		"categories": categories,
		"modified": doc.modified,
	}
	if include_media:
		payload["images"] = [
			{"image": row.image, "display_order": _media_display_order(row, index)}
			for index, row in _ordered_media(doc.get("images") or [])
			if row.image
		]
		payload["videos"] = [
			{"label": row.label, "url": row.url, "playback_type": "file" if _is_uploaded_product_video(row.url or "") else "external", "display_order": _media_display_order(row, index)}
			for index, row in _ordered_media(doc.get("videos") or [])
		]
	return payload


def _parent_product_payload(doc):
	payload = _product_payload(doc, include_media=True)
	return {
		"name": payload["name"],
		"product_name": payload["product_name"],
		"description": payload["description"],
		"unit_price": payload["unit_price"],
		"primary_category": payload["primary_category"],
		"category": payload["category"] if payload["category"] and payload["category"]["active"] else None,
		"categories": [category for category in payload["categories"] if category["active"]],
		"images": payload.get("images") or [],
		"videos": payload.get("videos") or [],
	}


def _category_payload(doc):
	return {
		"name": doc.name,
		"category_name": doc.category_name or "",
		"display_order": cint(doc.display_order),
		"active": cint(doc.active),
		"modified": doc.get("modified"),
	}


def _category_payload_for_product(category):
	if not category or not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category):
		return None
	return _category_payload(frappe.get_cached_doc(PRODUCT_CATEGORY_DOCTYPE, category))


def _product_category_names(doc):
	names = [row.get("category") for row in doc.get("categories") or [] if row.get("category")]
	if not names and doc.get("primary_category"):
		names = [doc.get("primary_category")]
	return list(dict.fromkeys(names))


def _apply_product_categories(doc, data):
	if "category_ids" in data:
		names = data["category_ids"]
		if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
			frappe.throw(_("Product categories must be a list of category names."))
	elif "primary_category" in data:
		# Older clients may update the primary category without knowing the other memberships.
		names = [data["primary_category"]] + [name for name in _product_category_names(doc) if name != doc.get("primary_category")]
	else:
		names = _product_category_names(doc)
	validated = list(dict.fromkeys(category for name in names if (category := _valid_product_category(name))))
	doc.set("categories", [{"category": name} for name in validated])
	doc.primary_category = validated[0] if validated else None


def _valid_product_category(category):
	category = str(category or "").strip()
	if category and not frappe.db.exists(PRODUCT_CATEGORY_DOCTYPE, category):
		frappe.throw(_("Product category was not found."))
	return category or None


def _ordered_media(rows):
	indexed = list(enumerate(rows))
	return sorted(indexed, key=lambda entry: (_media_display_order(entry[1], entry[0]), entry[0]))


def _media_display_order(row, fallback):
	value = row.get("display_order")
	return cint(value) if value is not None else fallback


def _order_payload(doc, include_items=True):
	from qas_custom.modules.notifications.store_order_notifications import notification_status
	notification = notification_status(doc)
	payload = {
		"name": doc.name,
		"parent": doc.parent,
		"parent_name": frappe.db.get_value("Parent", doc.parent, "parent_name") or doc.parent,
		"customer": doc.customer,
		"status": doc.status,
		"invoice": doc.invoice,
		"order_total": sum(flt(row.amount) for row in doc.get("items") or []),
		"pickup_address": frappe.db.get_value("Campus", doc.pickup_campus, "address") if doc.pickup_campus else "",
		"ready_notification_status": notification["status"],
		"ready_notification_error": notification["error"],
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
			if _is_uploaded_product_video(url):
				if not frappe.db.exists("File", {"file_url": url, "attached_to_doctype": PRODUCT_DOCTYPE, "attached_to_name": doc.name, "is_private": 0}):
					frappe.throw(_("Uploaded video must belong to this product."))
			elif not _safe_url(url):
				frappe.throw(_("Use an uploaded MP4 or a valid http or https video URL."))
			if not label:
				frappe.throw(_("Each video needs a label."))
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
			str(payload.get("pickup_campus") or ""),
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


def _require_parent_shop_testing():
	from qas_custom.config.shop_testing import require_parent_shop_testing

	if frappe.session.user == "Guest":
		frappe.throw(_("Login required."), frappe.PermissionError)
	require_parent_shop_testing()
	parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
	if not parent_name:
		frappe.throw(_("No parent record is linked to this account."), frappe.PermissionError)
	return frappe.get_cached_doc("Parent", parent_name)


def _has_field(doctype, fieldname):
	return frappe.db.exists("DocField", {"parent": doctype, "fieldname": fieldname})


def _safe_fields(doctype, names):
	return [name for name in names if name == "name" or _has_field(doctype, name)]


def _set_if_field(doc, fieldname, value):
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


def _limit(value, default, maximum):
	return max(1, min(cint(value or default), maximum))
