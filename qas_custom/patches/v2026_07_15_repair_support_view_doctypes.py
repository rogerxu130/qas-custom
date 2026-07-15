from __future__ import annotations

import frappe


SUPPORT_VIEW_DOCTYPES = ("Support View Token", "Support View Log")


def execute():
	for doctype in SUPPORT_VIEW_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		frappe.db.set_value("DocType", doctype, "module", "QAS Custom", update_modified=False)
		frappe.clear_cache(doctype=doctype)
	frappe.clear_cache()
