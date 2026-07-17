from qas_custom.modules.notifications.trial_parent_notifications import run_trial_parent_24h_reminders
from qas_custom.utils.environment import run_scheduled_or_skip


def send_trial_parent_24h_reminders():
	return run_scheduled_or_skip(
		"trial_parent_24h_reminders",
		run_trial_parent_24h_reminders,
	)
