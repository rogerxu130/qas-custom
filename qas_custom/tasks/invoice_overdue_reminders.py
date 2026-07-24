from qas_custom.modules.notifications.invoice_overdue_reminders import run_overdue_invoice_reminders
from qas_custom.utils.environment import run_scheduled_or_skip


def send_overdue_invoice_reminders():
	return run_scheduled_or_skip(
		"overdue_invoice_reminders",
		run_overdue_invoice_reminders,
	)
