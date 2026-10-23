import frappe

from qas_custom.services.teacher_accounts import create_teacher_account_data


@frappe.whitelist(methods=["POST"])
def school_admin_create_teacher_account(teacher_name=None, email=None, mobile=None):
	return create_teacher_account_data(teacher_name=teacher_name, email=email, mobile=mobile)
