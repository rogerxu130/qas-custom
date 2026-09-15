import frappe
from frappe.rate_limiter import rate_limit
from qas_custom.services import stripe_trial_payments as payments


@frappe.whitelist(allow_guest=True, methods=['GET'])
@rate_limit(limit=60, seconds=60)
def get_summary():
	return payments.summary(frappe.get_request_header('X-QAS-Payment-Token'))


@frappe.whitelist(allow_guest=True, methods=['POST'])
@rate_limit(limit=10, seconds=60)
def create_checkout(token=None):
	return payments.start_checkout(token)


@frappe.whitelist(allow_guest=True, methods=['POST'])
def receive_webhook():
	return payments.webhook()
