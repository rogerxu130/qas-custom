import frappe


OBSOLETE_DOCTYPES = ("Term Rollover Plan", "Term Rollover Plan Row")


def execute():
	"""Remove obsolete rollover doctypes replaced by planned Enrollment records."""
	for doctype in OBSOLETE_DOCTYPES:
		_delete_doctype_metadata(doctype)
		_drop_doctype_table(doctype)
	frappe.clear_cache()


def _delete_doctype_metadata(doctype):
	delete_specs = [
		("DocField", "parent"),
		("DocPerm", "parent"),
		("DocType Action", "parent"),
		("DocType Link", "parent"),
		("DocType State", "parent"),
		("Custom Field", "dt"),
		("Property Setter", "doc_type"),
	]
	for meta_doctype, fieldname in delete_specs:
		table = f"tab{meta_doctype}"
		if frappe.db.table_exists(table):
			frappe.db.sql(f"delete from `{table}` where `{fieldname}` = %s", doctype)

	if frappe.db.table_exists("tabDocType"):
		frappe.db.sql("delete from `tabDocType` where name = %s", doctype)


def _drop_doctype_table(doctype):
	table = f"tab{doctype}"
	if frappe.db.table_exists(table):
		frappe.db.sql_ddl(f"drop table `{table}`")
