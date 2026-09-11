from unittest import TestCase
from unittest.mock import patch, Mock
import frappe
from qas_custom.modules.billing.invoice_settings import course_invoice_due_date, apply_course_invoice_dates

MODULE='qas_custom.modules.billing.invoice_settings'

class TestCourseInvoiceDates(TestCase):
    def test_deadlines(self):
        for posting, first, mid, expected in [
            ('2026-09-11','2026-10-12',False,'2026-10-05'),
            ('2026-10-05','2026-10-12',False,'2026-10-05'),
            ('2026-10-06','2026-10-12',False,'2026-10-09'),
            ('2026-10-12','2026-10-12',False,'2026-10-15'),
            ('2026-10-15','2026-10-12',False,'2026-10-18'),
            ('2026-09-11','2026-10-19',True,'2026-09-14'),
            ('2026-12-30','2027-01-02',False,'2027-01-02'),
        ]:
            with self.subTest(posting=posting, first=first, mid=mid):
                self.assertEqual(str(course_invoice_due_date(posting,first,mid_term=mid)),expected)

    def test_combined_invoice_and_payment_schedule(self):
        enrollment=frappe._dict(name='E1',term='T',weekly_timeslot='W1',start_course_session='S1')
        other=frappe._dict(name='E2',term='T',weekly_timeslot='W2',start_course_session='S2')
        invoice=frappe._dict(docstatus=0,posting_date='2026-09-11',due_date='2026-09-18',items=[frappe._dict(enrollment='E1'),frappe._dict(enrollment='E2')],payment_schedule=[frappe._dict(due_date='2026-09-18')])
        def get_doc(kind,name):
            return other if kind=='Enrollment' else frappe._dict(start_date='2026-10-01',end_date='2026-12-31')
        with patch(MODULE+'.frappe.get_doc',side_effect=get_doc), patch(MODULE+'.frappe.get_all',return_value=[frappe._dict(session_date='2026-10-12')]), patch(MODULE+'.frappe.db',new=Mock()) as db:
            db.get_value.side_effect=lambda kind,name,field: {'S1':'2026-10-12','S2':'2026-10-19'}[name]
            apply_course_invoice_dates(invoice,enrollment=enrollment,start_session='S1')
        self.assertEqual(str(invoice.due_date),'2026-09-14')
        self.assertEqual(invoice.payment_schedule[0].due_date,invoice.due_date)
        self.assertEqual(invoice.posting_date,'2026-09-11')

    def test_submitted_invoice_is_unchanged(self):
        invoice=frappe._dict(docstatus=1,due_date='2026-09-18')
        apply_course_invoice_dates(invoice)
        self.assertEqual(invoice.due_date,'2026-09-18')

    def test_unlinked_manual_invoice_is_unchanged(self):
        invoice=frappe._dict(docstatus=0,posting_date='2026-09-11',due_date='2026-09-18',items=[])
        apply_course_invoice_dates(invoice)
        self.assertEqual(invoice.due_date,'2026-09-18')
