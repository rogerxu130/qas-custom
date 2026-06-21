from qas_custom.services.maintenance import (
	reconcile_attendance_links,
	run_nightly_maintenance,
	sync_student_activity_status,
)


def nightly_maintenance():
	return run_nightly_maintenance()


def nightly_sync_student_activity_status():
	return sync_student_activity_status()


def nightly_reconcile_attendance_links():
	return reconcile_attendance_links()
