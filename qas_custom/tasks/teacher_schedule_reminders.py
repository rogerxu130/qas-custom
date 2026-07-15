from qas_custom.modules.notifications.teacher_schedule_reminders import run_teacher_next_day_schedule_reminders
from qas_custom.utils.environment import run_scheduled_or_skip


def send_teacher_next_day_schedule_reminders():
	return run_scheduled_or_skip(
		"teacher_next_day_schedule_reminders",
		run_teacher_next_day_schedule_reminders,
	)
