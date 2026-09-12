from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import material_orders as orders
from qas_custom.modules.notifications import store_order_notifications as mail


class StoreOrderPickupTests(TestCase):
 def setUp(self):
  self.stack = ExitStack(); self.addCleanup(self.stack.close)
  self.db = SimpleNamespace(sql=Mock(), get_value=Mock(return_value='Family'), savepoint=Mock(), rollback=Mock(), exists=Mock(return_value=True))
  self.stack.enter_context(patch.object(frappe,'db',self.db))
  self.stack.enter_context(patch.object(frappe,'session',SimpleNamespace(user='staff')))
  self.stack.enter_context(patch.object(orders,'_',lambda x:x))
  self.stack.enter_context(patch.object(frappe,'throw',side_effect=ValueError))
  self.stack.enter_context(patch.object(orders,'now_datetime',return_value='2026-09-12 09:00:00'))
  self.stack.enter_context(patch.object(mail,'now_datetime',return_value='2026-09-12 09:00:00'))

 def doc(self, **values):
  doc=frappe._dict(name='STORE-ORD-2026-00001',parent='P1',status='Ordered',pickup_campus='North',items=[frappe._dict(amount=25)],invoice='LEGACY-INVOICE',**values)
  doc.insert=Mock();doc.save=Mock();doc.add_comment=Mock()
  doc.db_set=lambda updates,**kw:doc.update(updates)
  return doc

 def test_creation_without_customer_or_session_never_creates_invoice(self):
  doc=self.doc(); parent=frappe._dict(name='P1')
  with patch.object(orders,'_store_order_options',return_value={'pickup_campuses':[{'name':'North'}]}), patch.object(orders,'_store_order_items',return_value=[{'amount':25}]), patch.object(frappe,'get_doc',return_value=doc) as get, patch.object(orders,'_order_payload',return_value={'name':doc.name}):
   orders._create_store_order(parent,{'pickup_campus':'North','items':[{}]})
  data=get.call_args.args[0]
  self.assertEqual(data['doctype'],'Store Order');self.assertNotIn('invoice',data);self.assertNotIn('customer',data);self.assertNotIn('pickup_course_session',data)
  self.assertEqual(get.call_count,1);doc.insert.assert_called_once()

 def test_invalid_campus_rejected_before_inserting(self):
  with patch.object(orders,'_store_order_options',return_value={'pickup_campuses':[{'name':'North'}]}),patch.object(frappe,'get_doc') as get:
   with self.assertRaises(ValueError):orders._create_store_order(frappe._dict(name='P1'),{'pickup_campus':'South'})
   get.assert_not_called()

 def test_options_enrolled_campuses_default_and_admin_override(self):
  campuses=[frappe._dict(name='North'),frappe._dict(name='South')]
  for admin,expected in [(False,['South']),(True,['North','South'])]:
   with patch.object(frappe,'get_all',side_effect=[campuses,['T1'],['South']]),patch.object(orders,'_parent_payload',return_value={}):
    data=orders._store_order_options(frappe._dict(name='P1'),admin)
    self.assertEqual([x.name for x in data['pickup_campuses']],expected);self.assertEqual(data['default_campus'],'South')

 def test_no_enrollment_allows_active_campuses(self):
  with patch.object(frappe,'get_all',side_effect=[[frappe._dict(name='North')],[]]),patch.object(orders,'_parent_payload',return_value={}):
   data=orders._store_order_options(frappe._dict(name='P1'))
   self.assertEqual(data['default_campus'],'North')

 def test_line_price_uses_product_snapshot_without_creating_item(self):
  product=frappe._dict(active=1,name='KIT',product_name='Kit',unit_price=25)
  with patch.object(orders,'_get_product',return_value=product),patch.object(orders,'_ensure_material_item') as ensure:
   lines=orders._store_order_items([{'store_product':'KIT','qty':2,'unit_price':0}])
   self.assertEqual(lines[0]['amount'],50);ensure.assert_not_called()

 def test_duplicate_inactive_and_fractional_items_rejected(self):
  product=frappe._dict(active=1,name='KIT',product_name='Kit',unit_price=25)
  for rows in [[{'store_product':'KIT','qty':1.5}],[{'store_product':'KIT','qty':0}],[{'store_product':'KIT','qty':1}]*2]:
   with patch.object(orders,'_get_product',return_value=product),self.assertRaises(ValueError):orders._store_order_items(rows)
  product.active=0
  with patch.object(orders,'_get_product',return_value=product),self.assertRaises(ValueError):orders._store_order_items([{'store_product':'KIT','qty':1}])

 def transition(self,doc,status,notification):
  with patch.object(orders,'_require_school_admin'),patch.object(orders,'_locked_order',return_value=doc),patch.object(orders,'_order_payload',return_value={}),patch.object(mail,'queue_ready_notification',notification):
   orders.update_school_admin_store_order_status_data(doc.name,status)

 def test_ready_queues_notification_after_save(self):
  doc=self.doc(); notify=Mock(side_effect=lambda d: self.assertTrue(d.save.called))
  self.transition(doc,'Ready for collection',notify)
  self.assertEqual(doc.ready_by,'staff');notify.assert_called_once_with(doc)

 def test_walkin_collected_and_cancellation_never_send_or_touch_invoice(self):
  for status in ['Collected','Cancelled']:
   doc=self.doc();notify=Mock()
   with patch.object(frappe,'get_doc') as get:
    self.transition(doc,status,notify);get.assert_not_called()
   self.assertEqual(doc.status,status);self.assertEqual(doc.invoice,'LEGACY-INVOICE');notify.assert_not_called()

 def test_terminal_and_duplicate_transitions_rejected(self):
  for initial,target in [('Collected','Ready for collection'),('Cancelled','Collected'),('Ready for collection','Ready for collection')]:
   doc=self.doc();doc.status=initial
   with self.assertRaises(ValueError): self.transition(doc,target,Mock())

 def test_payload_total_independent_of_invoice(self):
  doc=self.doc();doc.items=[frappe._dict(amount=20),frappe._dict(amount=30)]
  with patch.object(mail,'notification_status',return_value={'status':'Not sent','error':''}):
   result=orders._order_payload(doc)
  self.assertEqual(result['order_total'],50)
  self.assertTrue(all(call.args[0]!='Sales Invoice' for call in self.db.get_value.call_args_list))

 def test_parent_detail_enforces_ownership(self):
  doc=self.doc()
  with patch.object(orders,'_require_parent_shop_testing',return_value=frappe._dict(name='OTHER')),patch.object(orders,'_get_order',return_value=doc),self.assertRaises(ValueError):orders.get_parent_store_order_data(doc.name)

 def test_parent_status_mutation_requires_admin(self):
  with patch.object(orders,'_require_school_admin',side_effect=PermissionError),patch.object(orders,'_locked_order') as lock,self.assertRaises(PermissionError):orders.update_school_admin_store_order_status_data('ORD','Collected')
  lock.assert_not_called()

 def test_ready_email_contains_escaped_number_campus_and_deep_link(self):
  doc=self.doc();doc.pickup_campus='<North>';self.db.get_value.return_value='<Address>'
  with patch.object(mail,'_parent_portal_url',side_effect=lambda p:'https://portal.example'+p):
   html=mail.ready_email_content(doc,{'parent_name':'<Parent>'})
  self.assertIn('&lt;North&gt;',html);self.assertIn('/shop?order=STORE-ORD-2026-00001',html);self.assertIn('normal opening hours',html);self.assertNotIn('<Address>',html)

 def test_ready_queue_uses_deferred_transport_and_records_queue(self):
  doc=self.doc()
  with patch.object(mail,'outbound_email_enabled',return_value=True),patch.object(mail,'_parent_recipient',return_value={'email':'parent@example.com'}),patch.object(mail,'ready_email_content',return_value='ready'),patch.object(mail,'sendmail_or_skip',return_value=SimpleNamespace(name='Q1')) as send:
   mail.queue_ready_notification(doc)
  self.assertEqual(doc.ready_email_queue,'Q1');self.assertFalse(send.call_args.kwargs['now']);self.assertTrue(send.call_args.kwargs['delayed'])

 def test_queue_failure_keeps_ready_and_exposes_retry(self):
  doc=self.doc();doc.status='Ready for collection'
  with patch.object(mail,'outbound_email_enabled',return_value=True),patch.object(mail,'_parent_recipient',return_value={'email':''}),patch.object(frappe,'log_error'),patch.object(frappe,'get_traceback',return_value='error'):
   mail.queue_ready_notification(doc)
  self.assertEqual(doc.status,'Ready for collection');self.assertEqual(mail.notification_status(doc)['status'],'Failed');self.db.rollback.assert_called_once()

 def test_sent_or_queued_email_not_duplicated(self):
  for status in ['Sent','Not Sent','Sending']:
   doc=self.doc(ready_email_queue='Q1');self.db.get_value.return_value=frappe._dict(status=status)
   with patch.object(mail,'sendmail_or_skip') as send:
    mail.queue_ready_notification(doc,retry=True);send.assert_not_called()

 def test_failed_transport_retries_original_queue(self):
  doc=self.doc(ready_email_queue='Q1');self.db.get_value.return_value=frappe._dict(status='Error')
  queue=SimpleNamespace(name='Q1',status='Error',retry_sending=Mock())
  with patch.object(mail,'outbound_email_enabled',return_value=True),patch.object(frappe,'get_doc',return_value=queue),patch.object(mail,'sendmail_or_skip') as send:
   mail.queue_ready_notification(doc,retry=True)
  queue.retry_sending.assert_called_once();send.assert_not_called()

 def test_staging_blocks_email_and_reports_skip(self):
  doc=self.doc()
  with patch.object(mail,'outbound_email_enabled',return_value=False),patch.object(mail,'email_block_reason',return_value='Staging'),patch.object(mail,'sendmail_or_skip') as send:
   mail.queue_ready_notification(doc)
  self.assertEqual(mail.notification_status(doc)['status'],'Skipped');send.assert_not_called()

 def test_admin_search_is_applied_before_limit_with_pagination(self):
  self.db.sql.return_value=[frappe._dict(name='OLD'),frappe._dict(name='NEXT')]
  with patch.object(orders,'_require_school_admin'),patch.object(frappe,'get_all',return_value=['North','South']),patch.object(frappe,'get_doc',side_effect=lambda doctype,name: name),patch.object(orders,'_order_payload',side_effect=lambda doc,**kw:{'name':doc}):
   result=orders.get_school_admin_store_orders_data(query="Family'",campus='South',status='Ordered',limit=1,start=80)
  sql,values=self.db.sql.call_args.args
  self.assertLess(sql.index('WHERE'),sql.index('LIMIT'));self.assertNotIn("Family'",sql)
  self.assertEqual(values['start'],80);self.assertEqual(values['query'],"%Family'%");self.assertEqual(values['campus'],'South')
  self.assertEqual(result['items'],[{'name':'OLD'}]);self.assertTrue(result['has_more']);self.assertEqual(result['campuses'],['North','South'])

 def test_doctypes_do_not_require_financial_or_session_links(self):
  import json
  from pathlib import Path
  root=Path(orders.__file__).parents[1]/'qas_custom'/'doctype'
  schema=json.loads((root/'store_order'/'store_order.json').read_text())
  fields={row['fieldname']:row for row in schema['fields']}
  self.assertFalse(fields['customer']['reqd']);self.assertFalse(fields['pickup_course_session']['reqd']);self.assertTrue(fields['pickup_campus']['reqd'])
  item=json.loads((root/'store_order_item'/'store_order_item.json').read_text())
  self.assertFalse(next(row for row in item['fields'] if row['fieldname']=='item_code')['reqd'])

 def test_expired_email_queue_does_not_block_order_or_duplicate_mail(self):
  import json
  from pathlib import Path
  doc=self.doc(ready_email_queue='PURGED');self.db.get_value.return_value=None
  self.assertEqual(mail.notification_status(doc)['status'],'History expired')
  with patch.object(mail,'sendmail_or_skip') as send:
   mail.queue_ready_notification(doc,retry=True);send.assert_not_called()
  schema=json.loads((Path(orders.__file__).parents[1]/'qas_custom/doctype/store_order/store_order.json').read_text())
  self.assertEqual(next(f for f in schema['fields'] if f['fieldname']=='ready_email_queue')['fieldtype'],'Data')
