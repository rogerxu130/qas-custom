from qas_custom.services.adhoc_booking import lock_due_bookings


def lock_due_adhoc_bookings():
	return lock_due_bookings()
