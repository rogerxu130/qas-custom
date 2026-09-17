# Run from a local bench sites directory. Fixed test site; always rolls back.
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import frappe
from frappe.utils import flt, nowdate, add_days
from unittest.mock import patch
frappe.init(site='qas-restore.test',sites_path='.')
frappe.connect()
frappe.set_user('Administrator')
frappe.flags.in_test=True
try:
 from qas_custom.modules.billing.invoice_reopen import reopen_same_invoice, reopened_invoice_revision
 from qas_custom.services.school_admin import _apply_school_admin_draft_invoice_payload
 rows=frappe.get_all('Sales Invoice',filters={'docstatus':1,'is_return':0,'update_stock':0,'grand_total':['>',0]},pluck='name',limit=20)
 template=next(frappe.get_doc('Sales Invoice',name) for name in rows if len(frappe.get_doc('Sales Invoice',name).items)>=1)
 doc=frappe.copy_doc(template)
 doc.set('items',[doc.items[0]])
 doc.flags.name_set=True
 doc.name='QAS-REOPEN-LOCAL-TEST'
 doc.amended_from=None
 doc.docstatus=0
 doc.status='Draft'
 doc.posting_date=template.posting_date
 doc.set_posting_time=1
 doc.due_date=add_days(doc.posting_date,15)
 doc.set('payment_schedule',[])
 doc.set('payments',[])
 doc.set('advances',[])
 doc.is_pos=0
 doc.paid_amount=0
 doc.qas_apply_store_credit_on_submit=0
 for field in ['enrollment','source_doctype','source_document','source_type','source_inquiry','parent']:
  if doc.meta.has_field(field):doc.set(field,None)
 for row in doc.items:
  for field in ['enrollment','sales_order','so_detail','delivery_note','dn_detail']:
   if row.meta.has_field(field):row.set(field,None)
 with patch('frappe.model.document.run_server_script_for_doc_event'), patch.object(frappe.db,'commit',side_effect=AssertionError('Unexpected commit in rollback-only integration test')), patch('frappe.sendmail',side_effect=AssertionError('Email attempted')):
  doc.insert(ignore_permissions=True)
  doc.submit()
  name=doc.name
  inquiry=frappe.db.get_value('Inquiry',{},'name') if frappe.db.exists('DocType','Inquiry') else None
  if inquiry:
   frappe.db.set_value('Inquiry',inquiry,'trial_invoice',name)
  revisions=[]
  original=doc.grand_total
  def balance():
   return flt(frappe.db.sql("select sum(debit-credit) from `tabGL Entry` where voucher_type='Sales Invoice' and voucher_no=%s and account=%s and is_cancelled=0",(name,doc.debit_to))[0][0])
  assert abs(balance()-original)<.01,(balance(),original)
  for cycle in (1,2):
   doc=reopen_same_invoice(doc,'Local rollback-only integration test')
   assert doc.name==name and doc.docstatus==0
   revisions.append(reopened_invoice_revision(name))
   assert revisions[-1]
   if inquiry: assert frappe.db.get_value('Inquiry',inquiry,'trial_invoice')==name
   assert abs(balance())<.01,balance()
   assert all(row.docstatus==0 for row in doc.get_all_children())
   new_due=add_days(doc.posting_date,20+cycle)
   _apply_school_admin_draft_invoice_payload(doc,{'due_date':new_due})
   doc.items[0].rate=50+cycle
   doc.items[0].qty=1
   doc.discount_amount=5
   doc.apply_discount_on='Grand Total'
   doc.save(ignore_permissions=True)
   doc.submit()
   doc.reload()
   assert doc.name==name and doc.docstatus==1
   assert str(doc.due_date)==str(new_due),(doc.due_date,new_due)
   assert abs(balance()-doc.grand_total)<.01,(balance(),doc.grand_total)
   assert abs(doc.outstanding_amount-doc.grand_total)<.01,(doc.outstanding_amount,doc.grand_total)
   print('PASS cycle',cycle,': same invoice name; draft balance zero; submitted GL and outstanding equal revised total; due date retained')
  assert len(set(revisions))==2
  if inquiry: assert frappe.db.get_value('Inquiry',inquiry,'trial_invoice')==name
  assert frappe.db.count('Sales Invoice',{'amended_from':name})==0
  print('PASS no amendment invoices created; all test writes rolled back')
finally:
 frappe.db.rollback()
 frappe.destroy()
