from qas_custom.services.maintenance import (
	reconcile_attendance_links,
	run_nightly_maintenance,
	sync_student_activity_status,
)
from qas_custom.utils.environment import run_scheduled_or_skip


def nightly_maintenance():
	return run_scheduled_or_skip("nightly_maintenance", run_nightly_maintenance)


def nightly_sync_student_activity_status():
	return run_scheduled_or_skip("nightly_sync_student_activity_status", sync_student_activity_status)


def nightly_reconcile_attendance_links():
	return run_scheduled_or_skip("nightly_reconcile_attendance_links", reconcile_attendance_links)
