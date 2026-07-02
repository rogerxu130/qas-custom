from qas_custom.services.adhoc_booking import lock_due_bookings
from qas_custom.utils.environment import run_scheduled_or_skip


def lock_due_adhoc_bookings():
	return run_scheduled_or_skip("lock_due_adhoc_bookings", lock_due_bookings)
