from qas_custom.modules.notifications.teacher_session_completion_reminders import (
	run_teacher_session_completion_reminders,
)
from qas_custom.utils.environment import run_scheduled_or_skip


def send_teacher_session_completion_reminders():
	return run_scheduled_or_skip(
		"teacher_session_completion_reminders",
		run_teacher_session_completion_reminders,
	)
