"""Run the actual follow-up query against both supported invoice schemas.

Framework I/O is stubbed; SQL executes against relational fixtures.
Run: python3 -m unittest qas_custom.tests.test_payg_followup_sql
"""
import importlib.util
from datetime import date, datetime
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class Row(dict):
    __getattr__ = dict.get


class FollowupSqlTests(TestCase):
    def collect(self, parent_column=False, invoice_parent=None, customer='CUSTOMER'):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.create_function('IF', 3, lambda condition, yes, no: yes if condition else no)
        db.executescript('''
            CREATE TABLE `tabSales Invoice` (name TEXT, customer TEXT, customer_name TEXT,
                outstanding_amount REAL, currency TEXT, due_date TEXT, status TEXT,
                docstatus INT, is_return INT);
            CREATE TABLE `tabQAS PAYG Operation` (name TEXT, invoice TEXT, family_parent TEXT,
                customer TEXT, card TEXT, target_card TEXT, operation_type TEXT, status TEXT);
            CREATE TABLE `tabParent` (name TEXT, parent_name TEXT);
            CREATE TABLE `tabQAS PAYG Card` (name TEXT, issued_on TEXT);
            INSERT INTO `tabParent` VALUES ('FAMILY', 'Family name');
            INSERT INTO `tabQAS PAYG Operation` VALUES
                ('OP', 'INV', 'FAMILY', 'CUSTOMER', 'CARD', NULL, 'Purchase', 'Completed');
            INSERT INTO `tabQAS PAYG Card` VALUES ('CARD', '2026-10-10');
        ''')
        db.execute("INSERT INTO `tabSales Invoice` VALUES ('INV', ?, 'Customer name', 300, 'AUD', '2026-10-25', 'Unpaid', 1, 0)", (customer,))
        if parent_column:
            db.execute('ALTER TABLE `tabSales Invoice` ADD COLUMN parent TEXT')
            db.execute('UPDATE `tabSales Invoice` SET parent=?', (invoice_parent,))

        def sql(query, as_dict=False):
            cursor = db.execute(query)
            return [Row(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]

        fake = ModuleType('frappe')
        fake.db = SimpleNamespace(sql=sql, has_column=Mock(return_value=parent_column))
        fake.get_all = Mock(return_value=[])
        utils = ModuleType('frappe.utils')
        utils.date_diff = lambda a, b: (date.fromisoformat(str(a)) - date.fromisoformat(str(b))).days
        utils.flt = float
        utils.get_datetime_in_timezone = lambda zone: datetime(2026, 10, 18)
        school_admin = ModuleType('qas_custom.services.school_admin')
        school_admin._doctype_available = lambda doctype: True
        path = Path(__file__).parents[1] / 'services/payg_payment_followups.py'
        spec = importlib.util.spec_from_file_location('_payg_followup_sql_test', path)
        service = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'frappe': fake, 'frappe.utils': utils,
                                     'qas_custom.services.school_admin': school_admin}):
            spec.loader.exec_module(service)
            rows = service.collect_payg_payment_followups()
        fake.db.has_column.assert_called_once_with('Sales Invoice', 'parent')
        return rows

    def test_missing_parent_column_uses_operation_family(self):
        rows = self.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['parent'], 'FAMILY')
        self.assertEqual(rows[0]['customer_name'], 'Family name')
        self.assertEqual(rows[0]['outstanding_amount'], 300)
        self.assertEqual(rows[0]['cards'][0]['name'], 'CARD')
        self.assertNotIn('invoice_parent', rows[0])

    def test_existing_parent_column_keeps_family_guard(self):
        for parent in (None, '', 'FAMILY'):
            with self.subTest(parent=parent):
                self.assertEqual(len(self.collect(True, parent)), 1)
        self.assertEqual(self.collect(True, 'ANOTHER-FAMILY'), [])

    def test_customer_mismatch_is_rejected_in_both_schemas(self):
        for has_parent in (False, True):
            with self.subTest(has_parent=has_parent):
                self.assertEqual(self.collect(has_parent, 'FAMILY', 'ANOTHER-CUSTOMER'), [])
