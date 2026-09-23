"""Scheduled PAYG reservation consumption, one booking per transaction."""
import frappe
from frappe.utils import get_datetime_in_timezone

from qas_custom.modules.payg.cancellation import lock_due_booking
from qas_custom.utils.environment import run_scheduled_or_skip


def _now():
    return get_datetime_in_timezone("Australia/Brisbane")


def _lock_due_payg_bookings(batch_size=200):
    cursor = ("1000-01-01 00:00:00", "")
    locked = []
    while True:
        rows = frappe.db.sql("""SELECT name, cancellable_until FROM `tabQAS PAYG Booking`
            WHERE status='Reserved' AND cancellable_until<=%s
              AND (cancellable_until>%s OR (cancellable_until=%s AND name>%s))
            ORDER BY cancellable_until, name LIMIT %s""",
            (_now(), cursor[0], cursor[0], cursor[1], batch_size), as_dict=True)
        if not rows:
            break
        for row in rows:
            cursor = (row.cancellable_until, row.name)
            try:
                if lock_due_booking(row.name):
                    locked.append(row.name)
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
                try:
                    frappe.log_error(frappe.get_traceback(), f"PAYG booking lock failed: {row.name}")
                except Exception:
                    frappe.logger("qas_custom").exception("PAYG booking lock failed: %s", row.name)
        if len(rows) < batch_size:
            break
    return {"locked": locked}


def lock_due_payg_bookings():
    return run_scheduled_or_skip("lock_due_payg_bookings", _lock_due_payg_bookings)
