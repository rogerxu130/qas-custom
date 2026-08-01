"""Extend active legacy 90-day makeup vouchers to the one-year policy."""

import frappe
from frappe.utils import add_days, getdate, today

from qas_custom.modules.makeup.commands import (
	DEFAULT_VOUCHER_EXPIRY_DAYS,
	LEGACY_VOUCHER_EXPIRY_DAYS,
)


def execute():
	if not frappe.db.exists("DocType", "Makeup Voucher"):
		return

	today_date = getdate(today())
	vouchers = frappe.get_all(
		"Makeup Voucher",
		filters={
			"status": "Valid",
			"expiry_date": [">=", today_date],
		},
		fields=["name", "issue_date", "expiry_date"],
		limit_page_length=0,
	)

	for voucher in vouchers:
		if not voucher.get("issue_date") or not voucher.get("expiry_date"):
			continue
		issue_date = getdate(voucher.issue_date)
		legacy_expiry_date = getdate(add_days(issue_date, LEGACY_VOUCHER_EXPIRY_DAYS))
		if getdate(voucher.expiry_date) != legacy_expiry_date:
			continue
		frappe.db.set_value(
			"Makeup Voucher",
			voucher.name,
			"expiry_date",
			add_days(issue_date, DEFAULT_VOUCHER_EXPIRY_DAYS),
			update_modified=False,
		)

	frappe.clear_cache(doctype="Makeup Voucher")
