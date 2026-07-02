from __future__ import annotations

from typing import Iterable

import frappe
from frappe.utils import cint, now_datetime


STAGING_ENVIRONMENTS = {"staging", "stage", "test", "testing", "qa", "sandbox"}


def qas_environment() -> str:
	value = (
		frappe.conf.get("qas_environment")
		or frappe.conf.get("environment")
		or frappe.conf.get("env")
		or "production"
	)
	return str(value).strip().lower() or "production"


def is_staging_environment() -> bool:
	return qas_environment() in STAGING_ENVIRONMENTS


def outbound_email_enabled() -> bool:
	if not is_staging_environment():
		return True
	return cint(frappe.conf.get("qas_staging_allow_email") or frappe.conf.get("qas_allow_outbound_email")) == 1


def scheduler_enabled() -> bool:
	if not is_staging_environment():
		return True
	return cint(frappe.conf.get("qas_staging_allow_scheduler") or frappe.conf.get("qas_allow_scheduler")) == 1


def payment_mutations_enabled() -> bool:
	if not is_staging_environment():
		return True
	return cint(frappe.conf.get("qas_staging_allow_payment_mutations") or frappe.conf.get("qas_allow_payment_mutations")) == 1


def email_block_reason() -> str:
	return f"Outbound email disabled for QAS {qas_environment()} environment."


def scheduler_block_reason() -> str:
	return f"Scheduled task disabled for QAS {qas_environment()} environment."


def payment_block_reason() -> str:
	return f"Payment mutation disabled for QAS {qas_environment()} environment."


def sendmail_or_skip(*, action: str, recipients: Iterable[str] | None = None, **kwargs):
	recipients = list(recipients or kwargs.get("recipients") or [])
	if outbound_email_enabled():
		return frappe.sendmail(recipients=recipients, **kwargs)

	_log_staging_skip(
		action=action,
		reason=email_block_reason(),
		details={"recipients": recipients, "subject": kwargs.get("subject")},
	)
	return {"skipped": True, "reason": email_block_reason(), "recipients": recipients}


def run_scheduled_or_skip(action: str, callback, *args, **kwargs):
	if scheduler_enabled():
		return callback(*args, **kwargs)

	reason = scheduler_block_reason()
	_log_staging_skip(action=action, reason=reason)
	return {"skipped": True, "reason": reason}


def _log_staging_skip(*, action: str, reason: str, details: dict | None = None):
	try:
		frappe.logger("qas_custom").info(
			"QAS staging safety skipped %s at %s: %s %s",
			action,
			now_datetime(),
			reason,
			details or {},
		)
	except Exception:
		pass
