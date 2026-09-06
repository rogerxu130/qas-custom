import frappe
from qas_custom.services import concentrated_makeup as service


@frappe.whitelist(methods=["GET"])
def get_options(student=None):
    return service.get_options(student)


@frappe.whitelist(methods=["POST"])
def book(voucher_id, session_id, student):
    return service.book(voucher_id, session_id, student)


@frappe.whitelist(methods=["GET"])
def get_settings(course_session):
    return service.get_settings(course_session)


@frappe.whitelist(methods=["POST"])
def update_settings(course_session, enabled, capacity):
    return service.update_settings(course_session, enabled, capacity)
