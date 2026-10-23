"""Create linked Teacher Portal accounts without granting Desk access."""
import frappe
from frappe import _
from frappe.utils import validate_email_address


def create_teacher_account_data(teacher_name=None, email=None, mobile=None):
	_require_school_admin()
	teacher_name = _text(teacher_name, _("Teacher name"), required=True)
	email = _text(email, _("Login email"), required=True).lower()
	mobile = _text(mobile, _("Mobile"))
	if any(character.isspace() or character in ",;<>" for character in email):
		frappe.throw(_("Enter one valid login email address."))
	validate_email_address(email, throw=True)
	if frappe.db.exists("User", email) or frappe.db.exists("User", {"email": email}):
		frappe.throw(_("A login account already exists for this email. Link it in School Setup > Teachers instead."))
	if frappe.db.exists("Teacher", {"user": email}) or frappe.db.exists("Teacher", {"email": email}):
		frappe.throw(_("A teacher already uses this email. Open the existing teacher profile instead."))

	frappe.db.savepoint("create_teacher_account")
	try:
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = teacher_name
		user.enabled = 1
		user.user_type = "Website User"
		user.send_welcome_email = 0
		user.insert(ignore_permissions=True)
		if user.user_type != "Website User":
			frappe.throw(_("The new account must be a Website User. Check the default User roles in Frappe."))

		teacher = frappe.new_doc("Teacher")
		teacher.teacher_name = teacher_name
		teacher.email = email
		teacher.mobile = mobile
		teacher.status = "Active"
		teacher.user = user.name
		teacher.insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="create_teacher_account")
		raise

	return {
		"name": teacher.name,
		"teacher_name": teacher.teacher_name,
		"email": email,
		"mobile": mobile,
		"status": teacher.status,
		"user": user.name,
	}


def _text(value, label, required=False):
	if value is None:
		value = ""
	if not isinstance(value, str):
		frappe.throw(_("{0} must be text.").format(label))
	value = value.strip()
	if required and not value:
		frappe.throw(_("{0} is required.").format(label))
	if len(value) > 140:
		frappe.throw(_("{0} must be 140 characters or fewer.").format(label))
	return value


def _require_school_admin():
	from qas_custom.services.school_admin import _require_school_admin as require_school_admin
	from qas_custom.services.support_view import reject_support_view_write

	reject_support_view_write()
	require_school_admin()
