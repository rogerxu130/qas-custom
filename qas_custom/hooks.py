app_name = "qas_custom"
app_title = "QAS Custom"
app_publisher = "Queensland Art Center"
app_description = "Custom app for QAS Parent Portal and backend extensions"
app_email = "queenslandartschool@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "qas_custom",
# 		"logo": "/assets/qas_custom/logo.png",
# 		"title": "QAS Custom",
# 		"route": "/qas_custom",
# 		"has_permission": "qas_custom.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/qas_custom/css/qas_custom.css"
# app_include_js = "/assets/qas_custom/js/qas_custom.js"

# include js, css files in header of web template
# web_include_css = "/assets/qas_custom/css/qas_custom.css"
# web_include_js = "/assets/qas_custom/js/qas_custom.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "qas_custom/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "qas_custom/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
jinja = {
	"methods": [
		"qas_custom.utils.jinja_methods.qas_invoice_print_amounts",
	],
}

# Installation
# ------------

# before_install = "qas_custom.install.before_install"
# after_install = "qas_custom.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "qas_custom.uninstall.before_uninstall"
# after_uninstall = "qas_custom.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "qas_custom.utils.before_app_install"
# after_app_install = "qas_custom.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "qas_custom.utils.before_app_uninstall"
# after_app_uninstall = "qas_custom.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "qas_custom.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Email Queue": {
		"before_insert": "qas_custom.modules.notifications.guard.suppress_legacy_invoice_email_queue",
	},
	"Sales Invoice": {
		"before_validate": "qas_custom.modules.billing.store_credit.enforce_exact_draft_invoice_total",
		"before_insert": "qas_custom.modules.notifications.guard.disable_sales_invoice_auto_notifications",
		"before_submit": "qas_custom.modules.notifications.guard.disable_sales_invoice_auto_notifications",
		"on_submit": "qas_custom.modules.billing.store_credit.apply_store_credit_on_sales_invoice_submit",
		"on_update": "qas_custom.modules.notifications.guard.purge_legacy_invoice_email_queue",
		"on_change": "qas_custom.modules.notifications.guard.purge_legacy_invoice_email_queue",
	},
	"Payment Entry": {
		"on_submit": "qas_custom.modules.billing.store_credit.grant_store_credit_bonus_on_payment_entry_submit",
	},
	"Student": {
		"validate": "qas_custom.services.display_labels.sync_student_code",
	},
	"Parent": {
		"after_insert": "qas_custom.services.parent_customer.ensure_parent_customer_after_save",
		"on_update": "qas_custom.services.parent_customer.ensure_parent_customer_after_save",
	},
	"Leave Request": {
		"after_insert": "qas_custom.modules.makeup.commands.process_leave_request_after_insert",
	},
	"Makeup Voucher": {
		"on_update": "qas_custom.modules.makeup.commands.sync_makeup_voucher_attendance_after_save",
	},
	"Weekly Timeslot": {
		"validate": "qas_custom.services.school_admin.validate_weekly_timeslot_document",
	},
	"Session Homework": {
		"validate": "qas_custom.services.session_homework.validate_session_homework",
	},
	"Session Photo Post": {
		"validate": "qas_custom.services.session_photo_post.validate_session_photo_post",
	},
	"Session Video Post": {
		"validate": "qas_custom.services.session_video_post.validate_session_video_post",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"qas_custom.tasks.teacher_session_completion_reminders.send_teacher_session_completion_reminders",
			"qas_custom.tasks.trial_parent_notifications.send_trial_parent_24h_reminders",
		],
		"0 * * * *": [
			"qas_custom.tasks.campus_admin_trial_digest.send_campus_admin_next_day_trial_digests",
			"qas_custom.tasks.teacher_schedule_reminders.send_teacher_next_day_schedule_reminders",
		],
		"0 9 * * *": [
			"qas_custom.tasks.invoice_overdue_reminders.send_overdue_invoice_reminders",
		],
	},
	"hourly": [
		"qas_custom.tasks.adhoc_booking_tasks.lock_due_adhoc_bookings",
	],
	"daily": [
		"qas_custom.tasks.maintenance_tasks.nightly_maintenance",
	],
}

# Testing
# -------

# before_tests = "qas_custom.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "qas_custom.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "qas_custom.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["qas_custom.utils.before_request"]
# after_request = ["qas_custom.utils.after_request"]

# Job Events
# ----------
# before_job = ["qas_custom.utils.before_job"]
# after_job = ["qas_custom.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"qas_custom.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
