from qas_custom.modules.notifications.campus_admin_trial_digest import run_campus_admin_next_day_trial_digests
from qas_custom.utils.environment import run_scheduled_or_skip


def send_campus_admin_next_day_trial_digests():
	return run_scheduled_or_skip(
		"campus_admin_next_day_trial_digests",
		run_campus_admin_next_day_trial_digests,
	)
