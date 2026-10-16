from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import order_invoices as orders
from qas_custom.modules.billing import store_credit


class OrderInvoiceTests(TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(in_test=False)
        patch.object(frappe, 'session', SimpleNamespace(user='staff')).start()
        self.throw = patch.object(frappe, 'throw', side_effect=ValueError).start()
        self.translate = patch.object(orders, '_', lambda value: value).start()
        self.addCleanup(patch.stopall)

    def test_discount_and_money_rounding(self):
        rows = [{'unit_price': 50, 'qty': 2}]
        self.assertEqual(orders.totals(rows, 'Percent', 10, 'Family discount')['order_total'], 90)
        self.assertEqual(orders.totals(rows, 'Amount', 30, 'Discount')['order_total'], 70)
        self.assertEqual(orders.money('1.005'), 1.01)

    def test_invalid_discounts_rejected(self):
        for kind, value, reason in [('Percent',101,'reason'),('Amount',101,'reason'),('Amount',10,''),('',10,'reason'),('Amount',-1,'reason'),('Bogus',0,'')]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                orders.totals([{'unit_price':50,'qty':2}],kind,value,reason)
        for value in ['nan','inf','-inf','bad']:
            with self.subTest(value=value), self.assertRaises(ValueError): orders.money(value)

    def test_documents_and_dicts_share_amount_calculation(self):
        row = Mock(); row.get.side_effect = {'unit_price':25, 'qty':2}.get
        self.assertEqual(orders.totals([row])['order_total'], 50)

    def test_order_credit_ban_precedes_manual_override_and_course_lines(self):
        for extra in [{'qas_is_manual_invoice':1,'qas_apply_store_credit_on_submit':1}, {'items':[{'qas_line_type':'Course Fee'}]}]:
            invoice=frappe._dict(qas_invoice_type='Store Order', **extra)
            self.assertEqual(store_credit._store_credit_application_eligibility(invoice)[0], False)
            self.assertFalse(store_credit._is_course_invoice(invoice))
        invoice=frappe._dict(qas_invoice_type='Other',source_doctype='Store Order',qas_is_manual_invoice=1,qas_apply_store_credit_on_submit=1)
        self.assertFalse(store_credit._store_credit_application_eligibility(invoice)[0])

    def test_campus_access_cannot_cross_assignments(self):
        orders._access(SimpleNamespace(pickup_campus='North'), ['North'])
        with self.assertRaises(ValueError): orders._access(SimpleNamespace(pickup_campus='South'), ['North'])

    def test_payment_and_order_versions_invalidate_confirmation(self):
        doc=SimpleNamespace(modified='v1')
        with patch.object(orders,'financials',return_value={'paid_amount':30}): token=orders._token(doc, {'qty':1})
        with patch.object(orders,'financials',return_value={'paid_amount':40}), self.assertRaises(ValueError): orders._confirm(doc,{'qty':1},token)
        doc.modified='v2'
        with patch.object(orders,'financials',return_value={'paid_amount':30}), self.assertRaises(ValueError): orders._confirm(doc,{'qty':1},token)

    def test_refund_preview_is_only_a_reminder(self):
        changes={'items':[{'unit_price':50,'qty':2}],'discount_type':'Amount','discount_value':30,'discount_reason':'Discount'}
        with patch.object(orders,'financials',return_value={'paid_amount':100}),patch.object(orders,'_token',return_value='confirmed'):
            result=orders._preview(SimpleNamespace(),changes)
        self.assertEqual(result['refund_reminder'],30)
        self.assertEqual(result['remaining_amount'],0)
        self.assertEqual(result['confirmation'],'confirmed')

    def test_partial_payment_preview_leaves_correct_balance(self):
        changes={'items':[{'unit_price':50,'qty':1}],'discount_type':'','discount_value':0,'discount_reason':''}
        with patch.object(orders,'financials',return_value={'paid_amount':30}),patch.object(orders,'_token',return_value='confirmed'):
            result=orders._preview(SimpleNamespace(),changes)
        self.assertEqual(result['remaining_amount'],20)
        self.assertEqual(result['refund_reminder'],0)

    def test_paid_revision_preserves_original_and_creates_only_difference(self):
        doc=frappe._dict(name='ORDER',invoice='ORIGINAL',status='Collected',items=[frappe._dict(unit_price=50,qty=2)],discount_type='',discount_value=0,discount_reason='')
        doc.add_comment=Mock(); doc.save=Mock()
        changes={'items':[{'unit_price':50,'qty':1}], 'discount_type':'', 'discount_value':0,'discount_reason':'','reason':'Quantity corrected'}
        def apply(d,c): d.update(c)
        invoice=Mock()
        with patch.object(orders,'financials',return_value={'paid_amount':80,'invoices':[]}),patch.object(orders,'_apply',side_effect=apply),patch.object(frappe,'get_doc',return_value=Mock(docstatus=1)),patch.object(orders,'_new_invoice',return_value=invoice) as new,patch.object(orders,'_mutation',side_effect=lambda fn:fn()),patch.object(orders,'_notify'):
            orders._revise(doc,changes)
        self.assertEqual(new.call_args.kwargs['adjustment'],-50)
        self.assertEqual(doc.invoice,'ORIGINAL')
        invoice.submit.assert_called_once()

    def test_draft_blocks_dispatch_but_issued_unpaid_does_not(self):
        doc=frappe._dict(name='ORDER',invoice='OI',status='Ordered',pending_revision=None)
        doc.save=Mock();doc.add_comment=Mock();doc.set=lambda key,value:doc.update({key:value})
        with patch.object(orders,'_scope',return_value=None),patch.object(orders,'_lock',return_value=doc),patch.object(orders,'financials',return_value={'invoice_status':'Draft'}),self.assertRaises(ValueError):
            orders.manage('status',{'order':'ORDER','status':'Dispatched'})
        with patch.object(orders,'_scope',return_value=None),patch.object(orders,'_lock',return_value=doc),patch.object(orders,'financials',return_value={'invoice_status':'Issued','paid_amount':0}),patch.object(orders,'order_payload',return_value={}),patch.object(orders,'now_datetime',return_value='now'),patch.object(frappe,'session',SimpleNamespace(user='staff')):
            orders.manage('status',{'order':'ORDER','status':'Dispatched'})
        self.assertEqual(doc.status,'Collected')

    def test_campus_admin_cannot_approve(self):
        with patch.object(orders,'_scope',return_value=['North']),patch.object(orders,'_lock',return_value=frappe._dict(status='Ordered')),self.assertRaises(ValueError):
            orders.manage('approve',{'order':'ORDER'})

    def test_order_print_never_infers_store_credit_from_reduced_outstanding(self):
        from qas_custom.modules.billing.invoice_amounts import resolve_invoice_print_amounts
        invoice=frappe._dict(name='OI',qas_invoice_type='Store Order',docstatus=1,grand_total=100,outstanding_amount=30,qas_store_credit_applied=70)
        result=resolve_invoice_print_amounts(invoice)
        self.assertEqual(result, {'total':100, 'store_credit_applied':0.0, 'payable_amount':30})

    def test_order_pdf_shows_discount_without_store_credit_column(self):
        from qas_custom.modules.notifications.commands import _invoice_pdf_html
        context={'invoice':'OI-26-00001','invoice_title':'Order Invoice','is_order_invoice':True,
            'total':70,'store_credit_applied':0,'payable_amount':40,'posting_date':'','due_date':'',
            'items':[{'student':'Paint set','description':'2 x 50','amount':100}],
            'adjustments':[{'student':'Discount','description':'Order discount','amount':-30}]}
        html=_invoice_pdf_html(context)
        self.assertIn('Order Invoice',html)
        self.assertIn('Order discount',html)
        self.assertIn('-30.00',html)
        self.assertIn('<th>Item</th>',html)
        self.assertNotIn('Store credit applied',html)

    def test_inactive_existing_product_can_be_discounted_or_reduced_but_not_increased(self):
        from qas_custom.services import material_orders
        product=frappe._dict(name='KIT',product_name='Kit',active=0,unit_price=50,item_code='ITEM')
        existing=[frappe._dict(store_product='KIT',qty=2,unit_price=50)]
        with patch.object(frappe,'db',SimpleNamespace(get_value=Mock(return_value='KIT'))), patch.object(material_orders,'_get_product',return_value=product),patch.object(material_orders,'_',lambda value:value):
            for qty in (1,2):
                self.assertEqual(material_orders._store_order_items([{'store_product':'KIT','qty':qty}],existing_rows=existing)[0]['qty'],qty)
            with self.assertRaises(ValueError): material_orders._store_order_items([{'store_product':'KIT','qty':3}],existing_rows=existing)

    def test_order_search_preserves_product_and_invoice_search(self):
        db=SimpleNamespace(sql=Mock(return_value=[]))
        with patch.object(orders,'_scope',return_value=None),patch.object(frappe,'db',db),patch.object(frappe,'get_all',return_value=[]):
            result=orders.manage('list',{'query':'paint'})
        sql,values=db.sql.call_args.args
        self.assertIn('tabStore Order Item',sql)
        self.assertIn('tabSales Invoice',sql)
        self.assertEqual(values['q'],'%paint%')
        self.assertEqual(result['items'],[])
