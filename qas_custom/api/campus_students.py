"""Name-search student details and delegated orders, scoped on every request."""
import frappe
from frappe import _
from qas_custom.services.campus_admin import get_campus_admin_current_term_enrollments_data


def _student_context(student):
    if not isinstance(student, str) or not student.strip():
        frappe.throw(_("Choose a student."), frappe.PermissionError)
    result = get_campus_admin_current_term_enrollments_data(query=student, student=student)
    if result.get("state", "ready") != "ready":
        frappe.throw(result.get("message") or _("Student search is unavailable."))
    rows = result["items"]
    if not rows:
        frappe.throw(_("This student has no enrollment in an active term in your assigned campuses."), frappe.PermissionError)
    guardian = frappe.db.get_value("Student", student, "guardian")
    if not guardian:
        frappe.throw(_("This student has no linked parent."))
    return rows, guardian


@frappe.whitelist(methods=["POST"])
def manage(action=None, payload=None):
    data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
    if not isinstance(data, dict) or action not in {"detail", "options", "create"}:
        frappe.throw(_("Invalid student request."))
    if action == "create":
        from qas_custom.services.support_view import reject_support_view_write
        reject_support_view_write()
    student = data.get("student")
    rows, guardian = _student_context(student)
    if action == "detail":
        from qas_custom.modules.notifications.makeup_parent_notifications import _parent_recipient
        recipient = _parent_recipient(guardian)
        return {"student": student, "student_name": rows[0]["student_name"], "term": ", ".join(dict.fromkeys(row["term"] for row in rows)),
                "enrollments": rows, "parent": {"name": guardian, "parent_name": recipient["parent_name"],
                "phone": frappe.db.get_value("Parent", guardian, "mobile_number") or "", "email": recipient["email"]}}
    campuses = {row["campus"] for row in rows}
    if action == "options":
        from qas_custom.services.material_orders import _product_payload
        products = frappe.get_all("Store Product", filters={"active": 1}, pluck="name", order_by="product_name", limit_page_length=0)
        active = frappe.get_all("Campus", filters={"name": ["in", list(campuses)], "status": "Active"}, pluck="name", order_by="name", limit_page_length=0)
        return {"items": [_product_payload(frappe.get_doc("Store Product", name), include_media=False) for name in products], "campuses": active}
    if data.get("pickup_campus") not in campuses:
        frappe.throw(_("Choose one of this student's assigned campuses."), frappe.PermissionError)
    if not data.get("creation_key"):
        frappe.throw(_("An order request key is required. Refresh and try again."))
    from qas_custom.services.material_orders import _create_store_order, _get_parent
    # Derive the family and accept only product choices, never client prices or discounts.
    order = _create_store_order(_get_parent(guardian), {key: data.get(key) for key in ("items", "pickup_campus", "creation_key")}, admin=True, student=student)
    return {"name": order["name"]}
