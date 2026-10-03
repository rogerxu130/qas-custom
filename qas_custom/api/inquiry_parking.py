import frappe

from qas_custom.services.inquiry_parking import change_parking, get_wait_days, validate_wait_days, SETTINGS


def require_school_admin():
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()


@frappe.whitelist()
def get_settings():
    require_school_admin()
    return {"parked_wait_days": get_wait_days()}


@frappe.whitelist(methods=["POST"])
def update_settings(parked_wait_days):
    require_school_admin()
    from qas_custom.services.support_view import reject_support_view_write
    reject_support_view_write()
    doc = frappe.get_single(SETTINGS)
    doc.parked_wait_days = validate_wait_days(parked_wait_days)
    doc.save()
    return {"parked_wait_days": doc.parked_wait_days}


@frappe.whitelist(methods=["POST"])
def school_admin_change_parking(inquiry, action, note=None):
    require_school_admin()
    from qas_custom.services.support_view import reject_support_view_write
    reject_support_view_write()
    return change_parking(inquiry, action, note)


@frappe.whitelist(methods=["POST"])
def campus_admin_change_parking(inquiry, action, note=None):
    from qas_custom.services.campus_admin import _require_inquiry_access, reject_support_view_write
    reject_support_view_write()
    _require_inquiry_access(inquiry)
    return change_parking(inquiry, action, note)
