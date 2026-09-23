"""PAYG database checks; run only on a dedicated, migrated test site.

Set QAS_PAYG_TEST_SITE=1 in that site's bench test process. This module never
connects to a site or runs migrate by itself. Real migration order and MariaDB
nullable-unique behavior remain unverified until run on that dedicated site.
"""

import os
from unittest import TestCase, skipUnless


@skipUnless(os.environ.get("QAS_PAYG_TEST_SITE") == "1", "dedicated PAYG test site required")
class TestPaygSiteSchema(TestCase):
    def test_metadata_and_indexes(self):
        import frappe

        from qas_custom.patches.v2026_09_23_payg_indexes import INDEXES, execute

        for doctype, _, _, _ in INDEXES:
            self.assertTrue(frappe.db.table_exists(doctype))
            self.assertTrue(frappe.get_meta(doctype).fields)
        execute()
        execute()  # must remain idempotent after the first migration
        for doctype, _, columns, unique in INDEXES:
            rows = frappe.db.sql(f"show index from `tab{doctype}`", as_dict=True)
            grouped = {}
            for row in rows:
                grouped.setdefault(row.Key_name, []).append(row)
            self.assertTrue(any(
                tuple(part.Column_name for part in sorted(parts, key=lambda part: part.Seq_in_index)) == columns
                and all((part.Non_unique == 0) == unique for part in parts)
                for parts in grouped.values()
            ), (doctype, columns, unique))

    def test_database_rejects_duplicate_idempotency_keys(self):
        import frappe

        cases = (
            ("QAS PAYG Booking", "request_key", "payg-schema-duplicate-booking"),
            ("QAS PAYG Entry", "operation_key", "payg-schema-duplicate-entry"),
            ("QAS PAYG Operation", "issue_request_key", "payg-schema-duplicate-issue"),
            ("QAS PAYG Operation", "invoice_request_key", "payg-schema-duplicate-invoice"),
        )
        for doctype, field, key in cases:
            frappe.db.sql("savepoint payg_schema_key_check")
            try:
                table = "tab" + doctype
                frappe.db.sql(f"insert into `{table}` (`name`, `{field}`) values (%s, %s)",
                              (key + "-1", key))
                with self.assertRaises(Exception):
                    frappe.db.sql(f"insert into `{table}` (`name`, `{field}`) values (%s, %s)",
                                  (key + "-2", key))
            finally:
                frappe.db.sql("rollback to savepoint payg_schema_key_check")

        frappe.db.sql("savepoint payg_schema_operation_check")
        try:
            frappe.db.sql("""insert into `tabQAS PAYG Operation`
                          (`name`, `operation_type`, `request_key`) values (%s, %s, %s)""",
                          ("payg-schema-op-1", "Purchase", "payg-schema-operation-key"))
            with self.assertRaises(Exception):
                frappe.db.sql("""insert into `tabQAS PAYG Operation`
                              (`name`, `operation_type`, `request_key`) values (%s, %s, %s)""",
                              ("payg-schema-op-2", "Purchase", "payg-schema-operation-key"))
        finally:
            frappe.db.sql("rollback to savepoint payg_schema_operation_check")
