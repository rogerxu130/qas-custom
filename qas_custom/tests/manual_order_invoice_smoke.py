"""Run from frappe-bench/sites using the bench Python. Local .test sites only.

Updates only the local Store Order schema/category, then rolls back all financial fixtures.
All notification functions are mocked; no actual payments or refunds are sent.
"""


def main():
    import os
    import frappe, traceback
    import faulthandler
    faulthandler.dump_traceback_later(60, repeat=True)
    from unittest.mock import patch
    site = os.environ.get('QAS_ORDER_TEST_SITE', 'qas-restore.test')
    if not site.endswith('.test'):
     raise RuntimeError('Order smoke checks are restricted to local .test sites')
    frappe.init(site=site);frappe.connect();frappe.set_user('Administrator')
    frappe.conf.qas_environment='test';frappe.conf.qas_staging_allow_email=0;frappe.conf.qas_allow_outbound_email=0;frappe.conf.qas_staging_allow_payment_mutations=1
    try:
     frappe.reload_doc('qas_custom','doctype','store_order')
     from qas_custom.patches.v2026_10_16_order_invoices import execute
     execute();frappe.db.commit()
     print('Schema/category migration OK')
     if not frappe.db.exists('Fiscal Year', {'year_start_date':['<=','2026-10-16'],'year_end_date':['>=','2026-10-16'],'disabled':0}):
      frappe.get_doc({'doctype':'Fiscal Year','year':'__OrderTest2026__','year_start_date':'2026-07-01','year_end_date':'2027-06-30'}).insert(ignore_permissions=True)
     from qas_custom.services import order_invoices as oi
     from qas_custom.services.material_orders import _ensure_material_item
     parent=frappe.get_all('Parent',pluck='name',limit=1)[0];campus=frappe.get_all('Campus',filters={'status':'Active'},pluck='name',limit=1)[0]
     product=frappe.get_doc({'doctype':'Store Product','product_name':'__ORDER_INVOICE_TEST__','unit_price':50,'active':1}).insert(ignore_permissions=True)
     product.item_code=_ensure_material_item(product);product.save(ignore_permissions=True)
     from qas_custom.modules.notifications import store_order_notifications as mail
     with patch.object(mail,'queue_new_order_admin_notification'),patch.object(mail,'queue_order_invoice_notification'),patch.object(mail,'queue_ready_notification'):
      order=oi.manage('create',{'parent':parent,'pickup_campus':campus,'items':[{'store_product':product.name,'qty':2}]})
      name=order['name'];print('Created draft',order['invoice'],order['order_total'],order['invoice_status'])
      order=oi.manage('approve',{'order':name});print('Issued',order['invoice_status'])
      order=oi.manage('status',{'order':name,'status':'Dispatched'});print('Unpaid dispatch',order['status'])
      data={'order':name,'pickup_campus':campus,'items':[{'store_product':product.name,'qty':1}],'reason':'Correct quantity'}
      preview=oi.manage('preview',data);order=oi.manage('edit',{**data,'confirmation':preview['confirmation']});print('Unpaid revision',order['order_total'],order['invoice_status'],len(order['invoices']))
      methods=frappe.get_all('Mode of Payment',filters={'enabled':1},pluck='name');print('Payment methods available',len(methods))
      for method in methods:
       if 'cash' in method.lower():break
      order=oi.manage('pay',{'order':name,'amount':30,'mode_of_payment':method,'modified':str(order['modified']),'expected_paid':0})
      print('Partial paid',order['paid_amount'],order['remaining_amount'])
      data={'order':name,'pickup_campus':campus,'items':[{'store_product':product.name,'qty':1}], 'discount_type':'Percent','discount_value':60,'discount_reason':'Test discount','reason':'Discount adjustment'}
      preview=oi.manage('preview',data);print('Refund confirmation',preview['refund_reminder'])
      order=oi.manage('edit',{**data,'confirmation':preview['confirmation']});print('Paid revision',order['order_total'],order['paid_amount'],order['remaining_amount'],len(order['invoices']))
      assert order['order_total']==20 and order['paid_amount']==30 and order['remaining_amount']==0
      assert frappe.db.count('Payment Entry',{'payment_type':'Pay','party':frappe.get_doc('Parent',parent).customer})==0
      print('PASS: no synthetic refund payment')
      # Create/edit an original Draft: same invoice identity, then approve.
      def new_order(issue=True):
       result=oi.manage('create',{'parent':parent,'pickup_campus':campus,'items':[{'store_product':product.name,'qty':2}]})
       return oi.manage('approve',{'order':result['name']}) if issue else result
      def edit_order(result,qty=1,**discount):
       data={'order':result['name'],'pickup_campus':campus,'items':[{'store_product':product.name,'qty':qty}],'reason':'Test revision',**discount}
       preview=oi.manage('preview',data)
       return oi.manage('edit',{**data,'confirmation':preview['confirmation']})
      draft=new_order(False); original_invoice=draft['invoice']
      with patch.object(oi,'_scope',return_value=[campus]):
       draft=edit_order(draft,qty=1)
      assert draft['invoice']==original_invoice and draft['invoice_status']=='Draft' and draft['order_total']==50
      draft=oi.manage('approve',{'order':draft['name']});assert draft['invoice_status']=='Issued'
      print('PASS: Campus Admin edits original Draft; School Admin issues it')
      with patch.object(oi,'_scope',return_value=[campus]):
       request=edit_order(draft,qty=2)
       assert request['pending_approval'] and request['order_total']==50
       try:oi.manage('approve',{'order':draft['name']})
       except frappe.PermissionError:pass
       else:raise AssertionError('Campus Admin approved its own revision')
      approval=oi.manage('preview_approval',{'order':draft['name']})
      revised=oi.manage('approve',{'order':draft['name'],'confirmation':approval['confirmation']})
      assert not revised['pending_approval'] and revised['order_total']==100 and revised['invoice_status']=='Issued'
      print('PASS: Campus Admin issued-invoice revision requires School Admin approval')
      # Cancellation retains financial history and creates no fictitious refund.
      partial=new_order();partial=oi.manage('pay',{'order':partial['name'],'amount':80,'mode_of_payment':method,'modified':str(partial['modified']),'expected_paid':0})
      partial=edit_order(partial,qty=1)
      assert partial['paid_amount']==80 and partial['order_total']==50
      cancel=oi.manage('preview_cancel',{'order':partial['name'],'reason':'Customer cancelled'})
      assert cancel['refund_reminder']==80
      cancelled=oi.manage('cancel',{'order':partial['name'],'reason':'Customer cancelled','confirmation':cancel['confirmation']})
      assert cancelled['status']=='Cancelled' and cancelled['order_total']==0 and cancelled['paid_amount']==80
      print('PASS: paid cancellation uses confirmation without refund registration')
      unpaid=new_order();unpaid=edit_order(unpaid,qty=1)
      unpaid=oi.manage('pay',{'order':unpaid['name'],'amount':30,'mode_of_payment':method,'modified':str(unpaid['modified']),'expected_paid':0})
      unpaid=edit_order(unpaid,qty=1,discount_type='Percent',discount_value=20,discount_reason='Family discount')
      assert unpaid['remaining_amount']==10 and unpaid['paid_amount']==30
      unpaid=oi.manage('pay',{'order':unpaid['name'],'amount':10,'mode_of_payment':method,'modified':str(unpaid['modified']),'expected_paid':30})
      assert unpaid['invoice_status']=='Paid' and unpaid['paid_amount']==40
      increased=edit_order(unpaid,qty=2)
      assert increased['order_total']==100 and increased['remaining_amount']==60
      increased=oi.manage('pay',{'order':increased['name'],'amount':60,'mode_of_payment':method,'modified':str(increased['modified']),'expected_paid':40})
      assert increased['invoice_status']=='Paid' and increased['paid_amount']==100
      print('PASS: collect remaining payment after reduction and supplemental invoice after increase')
      draft=new_order(False)
      cancel=oi.manage('preview_cancel',{'order':draft['name'],'reason':'Mistake'})
      cancelled=oi.manage('cancel',{'order':draft['name'],'reason':'Mistake','confirmation':cancel['confirmation']})
      assert cancelled['invoice_status']=='Cancelled'
      try:oi.manage('approve',{'order':draft['name']})
      except frappe.ValidationError:pass
      else:raise AssertionError('Cancelled Draft issued')
      print('PASS: cancelled Draft cannot be issued')
      from qas_custom.services import material_orders
      with patch.object(material_orders,'_require_parent_shop',return_value=frappe.get_doc('Parent',parent)):
       parent_order=material_orders.create_parent_store_order_data({'pickup_campus':campus,'items':[{'store_product':product.name,'qty':2}],'discount_type':'Percent','discount_value':100,'discount_reason':'injected'})
       assert parent_order['invoice_status']=='Issued' and parent_order['order_total']==100
       parent_listing=oi.parent_invoices()
       assert any(row['name']==parent_order['name'] for row in parent_listing['items'])
      print('PASS: parent self-service issues at full price and ignores injected discount')
      from uuid import uuid4
      creation={'parent':parent,'pickup_campus':campus,'items':[{'store_product':product.name,'qty':2}], 'creation_key':str(uuid4())}
      first=oi.manage('create',creation);second=oi.manage('create',creation)
      assert first['name']==second['name'] and first['invoice']==second['invoice']
      print('PASS: duplicate create request returns the same order and invoice')
      from qas_custom.services.school_admin import _get_invoice_rows, _draft_invoice_submit_filters
      assert all(row.get('qas_invoice_type')!='Store Order' for row in _get_invoice_rows(limit=100))
      assert 'Store Order' in _draft_invoice_submit_filters()['qas_invoice_type'][1]
      print('PASS: tuition list and batch approval exclude Order Invoices')
      # PDF authorization is tested without invoking the external PDF renderer.
      from qas_custom.modules.notifications import commands
      with patch.object(commands,'render_parent_invoice_pdf',return_value=b'%PDF-test'):
       oi.download_invoice(first['invoice'])
       assert frappe.local.response.filecontent==b'%PDF-test'
      print('PASS: administrator PDF authorization')


    except Exception:
     traceback.print_exc()
     raise
    finally:
     frappe.db.rollback();frappe.destroy();print('Test transactions rolled back')


if __name__ == "__main__":
    main()
