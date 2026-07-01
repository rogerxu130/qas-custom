from __future__ import annotations

import frappe

from qas_custom.modules.notifications.guard import (
	disable_sales_invoice_auto_notifications,
	purge_legacy_invoice_email_queue,
)


def execute():
	disable_sales_invoice_auto_notifications()
	deleted = purge_legacy_invoice_email_queue()
	if deleted:
		frappe.logger("qas_custom").info("Purged %s legacy invoice email queue rows", deleted)
