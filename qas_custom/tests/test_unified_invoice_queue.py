from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import order_invoices as orders, school_admin as admin, workshops


class UnifiedInvoiceQueueTests(TestCase):
    def setUp(self):
        for module in (orders, admin, workshops):
            patch.object(module, '_', lambda value: value).start()
        patch.object(frappe, 'throw', side_effect=ValueError).start()
        self.addCleanup(patch.stopall)

    def test_draft_selection_and_count_include_order_and_payg_types(self):
        with patch.object(admin, '_has_field', return_value=True), patch.object(admin, '_doctype_available', return_value=True), patch.object(frappe, 'db', Mock()) as db:
            db.sql.return_value = ['CANCELLED-ORDER-DRAFT']
            filters = admin._draft_invoice_submit_filters()
            self.assertEqual(filters, {'docstatus': 0, 'status': ['!=', 'Cancelled'], 'name': ['not in', ['CANCELLED-ORDER-DRAFT']]})
            with patch.object(frappe, 'get_all', return_value=[frappe._dict(total=3)]) as get_all:
                self.assertEqual(admin._count_sales_invoices(filters), 3)
            self.assertEqual(get_all.call_args.kwargs['filters'], filters)

    def test_order_and_payg_can_be_filtered_in_the_unified_list(self):
        for source in ['PAYG Card']:
            filters = {}
            admin._apply_invoice_source_filter(filters, source)
            self.assertEqual(filters, {'qas_invoice_type': source})

    def test_order_filter_uses_type_or_source_including_legacy_material_orders(self):
        with patch.object(admin, '_doctype_available', return_value=True), patch.object(admin, '_safe_fields', side_effect=lambda _, fields: fields), patch.object(admin, '_attach_invoice_display_names', side_effect=lambda rows: rows), patch.object(frappe, 'get_all', return_value=[]) as get_all:
            admin._get_invoice_rows(source='Store Order')
        self.assertEqual(get_all.call_args.kwargs['or_filters'], [['qas_invoice_type', 'in', ['Store Order', 'Material Order']], ['source_doctype', '=', 'Store Order']])

    def test_draft_list_keeps_search_names_and_excludes_cancelled_order_history(self):
        with patch.object(admin, '_doctype_available', return_value=True), patch.object(admin, '_safe_fields', side_effect=lambda _, fields: fields), patch.object(admin, '_get_invoice_outstanding_amount_filters', return_value=[]), patch.object(admin, '_draft_invoice_submit_filters', return_value={'docstatus': 0, 'name': ['not in', ['CANCELLED-DRAFT']]}), patch.object(admin, '_attach_invoice_display_names', side_effect=lambda rows: rows), patch.object(frappe, 'get_all', return_value=[]) as get_all:
            admin._get_invoice_rows(status='Draft', names=['ORDER-DRAFT', 'PAYG-DRAFT'])
        filters = get_all.call_args.kwargs['filters']
        self.assertIn(['Sales Invoice', 'name', 'in', ['ORDER-DRAFT', 'PAYG-DRAFT']], filters)
        self.assertIn(['name', 'not in', ['CANCELLED-DRAFT']], filters)
        self.assertFalse(any('qas_invoice_type' in condition for condition in filters))

    def test_outstanding_summary_uses_the_same_all_type_scope(self):
        with patch.object(admin, '_doctype_available', return_value=True), patch.object(admin, '_has_field', return_value=True), patch.object(admin, '_safe_fields', side_effect=lambda _, fields: fields), patch.object(admin, '_invoice_credit_payload', side_effect=lambda row: {'payable_amount': row['outstanding_amount']}), patch.object(frappe, 'get_all', return_value=[{'name': 'OI', 'outstanding_amount': 80}, {'name': 'PAYG', 'outstanding_amount': 200}]) as get_all:
            self.assertEqual(admin._get_outstanding_invoice_summary(), {'count': 2, 'amount': 280})
        self.assertEqual(get_all.call_args.kwargs['filters'], {'docstatus': 1, 'outstanding_amount': ['>', 0]})

    def test_invoice_search_matches_an_order_number(self):
        with patch.object(admin, '_safe_fields', side_effect=lambda _, fields: fields), patch.object(admin, '_search_doctype', return_value=[]) as search:
            admin._search_invoice_direct_names('OD-26-0005', 40)
        self.assertIn('source_document', search.call_args.args[3])

    def test_generic_submit_routes_order_drafts_through_order_service(self):
        doc = frappe._dict(name='OI', qas_invoice_type='Store Order')
        with patch.object(admin, '_require_school_admin'), patch.object(admin, 'lock_payg_operations_for_invoices', return_value={}), patch.object(frappe, 'get_doc', return_value=doc), patch.object(frappe, 'db', Mock()), patch.object(orders, 'submit_order_invoice', return_value={'queued': True}) as submit, patch.object(admin, '_build_invoice_payload', return_value={'name': 'OI', 'docstatus': 1}), patch.object(admin, '_lock_school_admin_draft_invoice') as tuition_lock:
            result = admin.submit_school_admin_invoice_data('OI', send_notifications=False)
        submit.assert_called_once_with('OI', send_notifications=0)
        tuition_lock.assert_not_called()
        self.assertEqual(result['notification'], {'queued': True})

    def test_generic_editor_cannot_reprice_an_order_while_issuing(self):
        with patch.object(admin, '_require_school_admin'), patch.object(admin, 'lock_payg_operations_for_invoices', return_value={}), patch.object(frappe, 'get_doc', return_value=frappe._dict(qas_invoice_type='Store Order')), patch.object(frappe, 'db', Mock()), patch.object(orders, 'submit_order_invoice') as submit, self.assertRaises(ValueError):
            admin.submit_school_admin_invoice_data('OI', payload={'items': []})
        submit.assert_not_called()

    def _order(self, **extra):
        doc = frappe._dict(name='ORDER', invoice='OI', status='Ordered', pending_revision=None)
        doc.update(extra)
        doc.add_comment = Mock()
        return doc

    def _invoice(self, **extra):
        invoice = frappe._dict(name='OI', docstatus=0, source_doctype='Store Order', source_document='ORDER')
        invoice.update(extra)
        invoice.submit = Mock()
        return invoice

    def test_issue_uses_current_order_invoice_and_respects_silent_submit(self):
        doc, invoice = self._order(), self._invoice()
        with patch.object(frappe, 'get_doc', return_value=invoice), patch.object(frappe, 'session', SimpleNamespace(user='staff')), patch.object(orders, '_mutation', side_effect=lambda fn: fn()), patch.object(orders, '_notify') as notify:
            orders._issue_draft(doc, invoice_name='OI', send_notifications=False)
        invoice.submit.assert_called_once()
        notify.assert_not_called()

    def test_issue_rejects_stale_cancelled_pending_foreign_or_submitted_invoices(self):
        cases = [(self._order(invoice='NEW-OI'), self._invoice()), (self._order(status='Cancelled'), self._invoice()), (self._order(pending_revision='{}'), self._invoice()), (self._order(), self._invoice(source_document='OTHER-ORDER')), (self._order(), self._invoice(docstatus=1))]
        for doc, invoice in cases:
            with self.subTest(doc=doc), patch.object(frappe, 'get_doc', return_value=invoice), patch.object(orders, '_mutation') as mutate, self.assertRaises(ValueError):
                orders._issue_draft(doc, invoice_name='OI')
            mutate.assert_not_called()

    def test_campus_cannot_issue_from_the_unified_queue(self):
        with patch.object(orders, '_scope', return_value=['North']), self.assertRaises(ValueError):
            orders.submit_order_invoice('OI')

    def test_order_submit_locks_its_order_and_reports_queue_without_claiming_delivery(self):
        doc, invoice = self._order(), self._invoice()
        doc.reload = Mock()
        doc.invoice_email_queue = 'MAIL'
        with patch.object(orders, '_scope', return_value=None), patch.object(frappe, 'get_doc', return_value=invoice), patch.object(orders, '_lock', return_value=doc) as lock, patch.object(orders, '_issue_draft') as issue, patch('qas_custom.modules.notifications.store_order_notifications.notification_status', return_value={'status': 'Queued', 'error': ''}):
            result = orders.submit_order_invoice('OI')
        lock.assert_called_once_with('ORDER', None)
        issue.assert_called_once_with(doc, invoice_name='OI', send_notifications=True)
        self.assertFalse(result['sent'])
        self.assertTrue(result['queued'])

    def test_order_invoices_cannot_be_consolidated_with_tuition(self):
        with self.assertRaises(ValueError):
            workshops._validate_invoice_consolidation([frappe._dict(qas_invoice_type='Store Order'), frappe._dict(qas_invoice_type='PAYG Card')])

    def test_bulk_issue_distinguishes_queued_from_failed_email(self):
        with patch.object(frappe, 'db', Mock()) as db, patch.object(admin, 'submit_school_admin_invoice_data', return_value={'docstatus': 1, 'notification': {'queued': True}}):
            db.exists.return_value = True
            db.get_value.return_value = 0
            row = admin._run_one_bulk_invoice_submit('OI')
        self.assertTrue(row['ok'])
        self.assertEqual(row['message'], 'Invoice submitted; email queued')
