import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import cint, get_url

from qas_custom.services.stripe_trial_payments import validate_accounts


class QASStripeSettings(Document):
	def validate(self):
		if not self.get_password('signing_secret', raise_exception=False):
			self.signing_secret = secrets.token_urlsafe(48)
		self.webhook_url = get_url('/api/method/qas_custom.api.stripe_trial_payments.receive_webhook')
		if self.mode not in ('Test', 'Live'):
			frappe.throw('Choose Test or Live mode.')
		if cint(self.enabled):
			prefix = self.mode.lower()
			key = self.get_password(prefix + '_secret_key', raise_exception=False) or ''
			webhook = self.get_password(prefix + '_webhook_secret', raise_exception=False) or ''
			if not key.startswith('sk_' + prefix + '_') or not webhook.startswith('whsec_'):
				frappe.throw('Enter a matching Stripe Secret Key and Webhook Signing Secret for this mode.')
			if not self.company or not self.clearing_account or not self.mode_of_payment:
				frappe.throw('Configure company, Stripe clearing account and mode of payment.')
			validate_accounts(self)
