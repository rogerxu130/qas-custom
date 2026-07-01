from qas_custom.modules.notifications.guard import purge_legacy_invoice_email_queue


def execute():
	purge_legacy_invoice_email_queue()
