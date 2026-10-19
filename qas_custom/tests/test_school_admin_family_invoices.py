from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.school_admin import _get_invoice_rows


class TestFamilyInvoices(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def mock(name, **kwargs):
            return self.stack.enter_context(patch('qas_custom.services.school_admin.' + name, **kwargs))
        mock('_', side_effect=lambda value: value)
        mock('_doctype_available', return_value=True)
        mock('_has_field', return_value=True)
        mock('_safe_fields', side_effect=lambda doctype, fields: fields)
        self.students = mock('_invoice_names_for_students', return_value={'tuition'})
        mock('_attach_invoice_display_names', side_effect=lambda rows: rows)
        mock('_invoice_row_payload', side_effect=lambda row, summary: row)
        self.stack.enter_context(patch('qas_custom.services.payment_collection_requests.get_invoice_payment_request_summaries', return_value={}))
        self.rows = [
            {'name': 'tuition', 'customer': 'family', 'parent': 'parent', 'qas_invoice_type': 'Course'},
            {'name': 'pass', 'customer': 'family', 'qas_invoice_type': 'Pay Go'},
            {'name': 'materials', 'customer': 'family', 'qas_invoice_type': 'Store Order'},
            {'name': 'legacy', 'parent': 'parent', 'qas_invoice_type': 'Material Order'},
            {'name': 'other', 'customer': 'another-family', 'qas_invoice_type': 'Store Order'},
        ]
        def get_all(doctype, **kwargs):
            def matches(row, field, op, value):
                if op == '=': return row.get(field) == value
                if op == 'in': return row.get(field) in value
                if op == 'not in': return row.get(field) not in value
                if op == '!=': return row.get(field) != value
                raise AssertionError(op)
            filters = kwargs['filters']
            return [row for row in self.rows if all(matches(row, field, *(value if isinstance(value, list) else ['=', value])) for field, value in filters.items()) and (not kwargs.get('or_filters') or any(matches(row, *condition) for condition in kwargs['or_filters']))]
        self.query = mock('frappe.get_all', side_effect=get_all)

    def test_family_includes_studentless_purchases_and_excludes_other_families(self):
        rows = _get_invoice_rows(customer='family', parent='parent', students=['student'], family_scope=True, limit=0)
        self.assertEqual([row['name'] for row in rows], ['tuition', 'pass', 'materials', 'legacy'])
        self.assertEqual(self.query.call_args.kwargs['limit'], 0)

    def test_family_without_student_invoices_still_has_purchases(self):
        self.students.return_value = set()
        rows = _get_invoice_rows(customer='family', students=['student'], family_scope=True)
        self.assertEqual([row['name'] for row in rows], ['tuition', 'pass', 'materials'])

    def test_student_only_context_remains_scoped(self):
        rows = _get_invoice_rows(students=['student'], family_scope=True)
        self.assertEqual([row['name'] for row in rows], ['tuition'])

    def test_empty_identity_never_returns_all_invoices(self):
        self.assertEqual(_get_invoice_rows(family_scope=True), [])
        self.query.assert_not_called()

    def test_standard_query_keeps_existing_identity_filters(self):
        rows = _get_invoice_rows(customer='family', parent='parent', students=['student'])
        self.assertEqual([row['name'] for row in rows], ['tuition'])
        self.assertEqual(self.query.call_args.kwargs['filters'], {'customer': 'family', 'parent': 'parent', 'name': ['in', ['tuition']]})

    def test_store_order_source_keeps_its_existing_filter(self):
        rows = _get_invoice_rows(customer="family", source="Store Order")
        self.assertEqual([row["name"] for row in rows], ["materials"])
