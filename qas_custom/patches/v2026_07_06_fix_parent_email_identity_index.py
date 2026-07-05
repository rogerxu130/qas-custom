from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Parent"):
		return

	_drop_parent_name_unique_indexes()
	frappe.reload_doc("qas_custom", "doctype", "parent")
	_drop_parent_name_unique_indexes()
	_ensure_linked_user_unique_index()
	frappe.clear_cache(doctype="Parent")


def _drop_parent_name_unique_indexes():
	for row in _unique_indexes_for_column("tabParent", "parent_name"):
		index_name = row.get("Key_name")
		if not index_name or index_name == "PRIMARY":
			continue
		frappe.db.sql(f"alter table `tabParent` drop index `{index_name}`")


def _ensure_linked_user_unique_index():
	if not frappe.db.has_column("Parent", "linked_user"):
		return
	if _unique_indexes_for_column("tabParent", "linked_user"):
		return
	frappe.db.sql("alter table `tabParent` add unique index `idx_parent_linked_user_unique` (`linked_user`)")


def _unique_indexes_for_column(table_name, column_name):
	return frappe.db.sql(
		f"""
		show index from `{table_name}`
		where Column_name = %s and Non_unique = 0
		""",
		(column_name,),
		as_dict=True,
	)
