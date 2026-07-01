from __future__ import annotations

from qas_custom.modules.notifications.guard import (
	disable_sales_invoice_auto_notifications,
	purge_legacy_invoice_email_queue,
)


def execute():
	disable_sales_invoice_auto_notifications()
	purge_legacy_invoice_email_queue()
