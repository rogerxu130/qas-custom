from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services import unified_outstanding_report as report, payg_payment_followups as followups
from qas_custom.modules.notifications import payg_unpaid_booking as warning
from qas_custom.modules.billing import invoice_settings as settings


class FamilyFollowupTests(TestCase):
    def setUp(self):
        self.parents=[{'name':'P','customer':'C','parent_name':'Family'}]
        self.today=date(2026,10,18)
    def invoice(self, **extra):
        return dict(name='INV',parent='P',customer='C',docstatus=1,qas_invoice_type='PAYG Card',outstanding_amount=450,due_date='2026-10-25',**extra)
    def build(self, invoices, payg=None, attendance=None, snapshot=None):
        return report.build_family_followups(snapshot or [],invoices,self.parents,payg or [],attendance or [],self.today)
    def test_pass_within_payment_period_is_not_due_until_booked(self):
        row=self.build([self.invoice()])[0]
        self.assertEqual(row['invoice_classification'],'Not Due')
        row=self.build([self.invoice()],[{'name':'INV','booking_count':1,'cards':[],'bookings':[{'name':'B','student':'S','student_name':'Ava'}]}])[0]
        self.assertEqual(row['invoice_classification'],'Outstanding')
        self.assertEqual(row['students'][0]['student_name'],'Ava')
        self.assertIn('Booked but unpaid',row['debt_reasons'])
    def test_pass_is_overdue_day_after_due_date(self):
        invoice=self.invoice();invoice['due_date']='2026-10-18'
        self.assertEqual(self.build([invoice])[0]['invoice_classification'],'Not Due')
        invoice['due_date']='2026-10-17'
        self.assertEqual(self.build([invoice])[0]['invoice_classification'],'Outstanding')
    def test_art_debt_is_visible_with_independent_attended_status(self):
        invoice=self.invoice();invoice.update(qas_invoice_type='Store Order',source_doctype='Store Order',outstanding_amount=129,order_status='Dispatched')
        snapshot=[{'parent_record':'P','name':'ROW','attendance_classification':'Attended','present_late_count':1,'students':[]}]
        row=self.build([invoice],snapshot=snapshot)[0]
        self.assertEqual(row['attendance_classification'],'Attended')
        self.assertEqual(row['debt_types'],['Art supplies'])
        self.assertIn('Dispatched but unpaid',row['debt_reasons'])
        self.assertEqual(row['outstanding_amount'],129)
    def test_merged_course_and_pass_invoice_balance_is_counted_once(self):
        invoice=self.invoice();invoice.update(qas_invoice_type='Mixed',outstanding_amount=900)
        row=self.build([invoice,invoice],[{'name':'INV','booking_count':2,'cards':[]}])[0]
        self.assertEqual(row['outstanding_amount'],900)
        self.assertEqual(row['debt_types'],['Course fees','Ten-class pass'])
        self.assertEqual(len(row['invoices']),1)
    def test_consolidated_passes_without_tuition_are_not_labeled_course_fees(self):
        invoice=self.invoice();invoice['qas_invoice_type']='Mixed'
        row=self.build([invoice],[{'name':'INV','booking_count':0,'non_payg_debt_types':[]}])[0]
        self.assertEqual(row['debt_types'],['Ten-class pass'])
        self.assertEqual(row['invoice_classification'],'Not Due')

    def test_pass_booking_and_past_attendance_support_weekday_filter(self):
        payg=[{'name':'INV','booking_count':1,'bookings':[{'student':'S','schedule':{'day_of_week':'Friday','course':'Art'}}]}]
        rows=self.build([self.invoice()],payg=payg,attendance=[{'parent':'P','student':'S','status':'Present','day_of_week':'Friday','course':'Art'}])
        self.assertEqual(len(report.filter_family_followups(rows,day_of_week='Friday')),1)
        self.assertEqual(report.filter_family_followups(rows,day_of_week='Monday'),[])
        self.assertEqual(len(rows[0]['students'][0]['class_schedule']),1)

    def test_payg_attendance_counts_without_regular_enrollment(self):
        row=self.build([self.invoice()],attendance=[{'parent':'P','student':'S','student_name':'Ava','status':'Absent'}])[0]
        self.assertEqual(row['attendance_classification'],'Absent')
        self.assertEqual(row['absent_count'],1)
    def test_paid_invoice_updates_old_snapshot_and_clears_warning(self):
        invoice=self.invoice();invoice['outstanding_amount']=0
        row=self.build([invoice],snapshot=[{'name':'ROW','parent_record':'P','invoice_classification':'Outstanding','outstanding_amount':450,'students':[]}])[0]
        self.assertEqual(row['outstanding_amount'],0)
        self.assertEqual(row['invoice_classification'],'Not Outstanding')
        self.assertEqual(row['debt_types'],[])
    def test_ambiguous_legacy_customer_is_not_assigned_to_first_family(self):
        self.parents.append({'name':'P2','customer':'C'})
        invoice=self.invoice();invoice['parent']=None
        self.assertEqual(self.build([invoice]),[])
    def test_filters_combine_attendance_debt_type_and_search(self):
        invoice=self.invoice();invoice.update(qas_invoice_type='Store Order',source_doctype='Store Order')
        rows=self.build([invoice],snapshot=[{'name':'ROW','parent_record':'P','attendance_classification':'Attended','students':[]}])
        self.assertEqual(len(report.filter_family_followups(rows,attendance='Attended',invoice='Outstanding',debt_type='Art supplies',query='INV')),1)
        self.assertEqual(report.filter_family_followups(rows,attendance='Absent'),[])
        self.assertEqual(report.filter_family_followups(rows,debt_type='Ten-class pass'),[])


class PaygReadModelTests(TestCase):
    def test_exchange_descendant_bookings_find_original_unpaid_purchase_once(self):
        source=frappe._dict(name='INV',customer='C',customer_name='Family',outstanding_amount=300,currency='AUD',due_date='2026-10-25',status='Unpaid',parent='P',parent_name='Family',card='OLD',target_card=None,operation_type='Purchase',issued_on='2026-10-10')
        data={
            'Sales Invoice Item':[],
            'QAS PAYG Operation':[frappe._dict(family_parent='P',source_card='OLD',target_card='NEW')],
            'QAS PAYG Booking':[frappe._dict(name='B',card='NEW',family_parent='P',student='S',course_session='CS',attendance_entry='ATT',status='Reserved')],
            'Course Sessions':[frappe._dict(name='CS',session_date='2026-10-30',weekly_timeslot='W')],
            'Weekly Timeslot':[frappe._dict(name='W',day_of_week='Friday',course='Art')],
            'Class Attendance Entry':[frappe._dict(name='ATT',status='To be started')],
            'Student':[frappe._dict(name='S',student_name='Ava')]}
        fake=SimpleNamespace(db=SimpleNamespace(sql=Mock(return_value=[source]),has_column=Mock(return_value=True)),get_all=Mock(side_effect=lambda dt,**kw:data[dt]))
        with patch.object(followups,'frappe',fake),patch('qas_custom.services.school_admin._doctype_available',return_value=True),patch.object(followups,'get_datetime_in_timezone',return_value=datetime(2026,10,18)):
            rows=followups.collect_payg_payment_followups()
        self.assertEqual(rows[0]['booking_count'],1)
        self.assertEqual(rows[0]['cards'][0]['days_since_issue'],8)
        self.assertEqual(rows[0]['bookings'][0]['student_name'],'Ava')
        self.assertEqual(rows[0]['overdue_days'],0)
        self.assertEqual(rows[0]['non_payg_debt_types'],[])
        self.assertEqual(rows[0]['bookings'][0]['schedule']['day_of_week'],'Friday')


class WarningTests(TestCase):
    def setUp(self):
        self.log=Mock()
        self.db=Mock();self.db.get_value.side_effect=lambda dt,*args,**kw:frappe._dict(card='CARD',family_parent='P',status='Reserved') if dt=='QAS PAYG Booking' else 'admin@example.test'
        self.db.exists.return_value=False
        self.fake=SimpleNamespace(db=self.db,get_doc=Mock(return_value=self.log),enqueue=Mock(),DuplicateEntryError=frappe.DuplicateEntryError)
        self.row={'name':'INV','parent':'P','customer_name':'Family','bookings':[{'name':'B'}],'booking_count':1,'outstanding_amount':300,'due_date':'2026-10-25'}
        patches=[patch.object(warning,'frappe',self.fake),patch.object(warning,'outbound_email_enabled',return_value=True),patch.object(warning,'sendmail_or_skip',return_value=frappe._dict(name='MAIL')),patch.object(followups,'unpaid_booking_warnings',return_value=[self.row]),patch('qas_custom.modules.notifications.commands._payg_school_admin_emails',return_value=['admin@example.test']),patch('qas_custom.modules.notifications.makeup_parent_notifications._parent_portal_url',side_effect=lambda path:'https://portal.example.test'+path)]
        for p in patches:p.start()
        self.addCleanup(patch.stopall)
    def test_queue_runs_only_after_booking_commits(self):
        warning.queue_warning('B')
        self.assertTrue(self.fake.enqueue.call_args.kwargs['enqueue_after_commit'])
    def test_only_school_admin_receives_financial_warning(self):
        self.assertEqual(warning.send_warning('B'),{'queued':['INV']})
        self.assertEqual(warning.sendmail_or_skip.call_args.kwargs['recipients'],['admin@example.test'])
        self.assertIn('A$300.00',warning.sendmail_or_skip.call_args.kwargs['message'])
        self.assertIn('record=INV',warning.sendmail_or_skip.call_args.kwargs['message'])
        self.assertTrue(self.fake.get_doc.call_args.args[0]['name'].startswith('payg-unpaid-'))
        self.log.db_insert.assert_called_once()
        self.log.insert.assert_not_called()
    def test_already_warned_card_and_invoice_is_not_mailed_again(self):
        self.db.exists.return_value=True
        self.assertEqual(warning.send_warning('B'),{'queued':[]})
        warning.sendmail_or_skip.assert_not_called()
    def test_concurrent_marker_collision_does_not_send_a_second_email(self):
        self.log.db_insert.side_effect=frappe.DuplicateEntryError
        self.assertEqual(warning.send_warning('B'),{'queued':[]})
        self.db.rollback.assert_called_once_with(save_point='payg_warning_marker')
        warning.sendmail_or_skip.assert_not_called()

    def test_paid_or_cancelled_booking_is_rechecked_before_email(self):
        followups.unpaid_booking_warnings.return_value=[]
        warning.send_warning('B');warning.sendmail_or_skip.assert_not_called()
        self.db.get_value.side_effect=lambda *args,**kw:frappe._dict(card='CARD',family_parent='P',status='Cancelled')
        self.assertEqual(warning.send_warning('B'),{'skipped':True})
    def test_test_environment_does_not_send_or_create_a_marker(self):
        warning.outbound_email_enabled.return_value=False
        warning.send_warning('B');self.fake.get_doc.assert_not_called();warning.sendmail_or_skip.assert_not_called()
    def test_failed_email_queue_raises_before_marker_transaction_can_commit(self):
        warning.sendmail_or_skip.return_value=None
        with self.assertRaises(ValueError):warning.send_warning('B')


class DeadlineTests(TestCase):
    def test_deadline_has_independent_validated_default(self):
        self.assertEqual(settings.validate_course_due_days(None,'payg_due_days'),7)
        self.assertEqual(settings.validate_course_due_days(10,'payg_due_days'),10)
        with patch.object(settings.frappe,'throw',side_effect=ValueError):
            for value in (0,-1,1.5,'bad'):
                with self.subTest(value=value),self.assertRaises(ValueError):settings.validate_course_due_days(value,'payg_due_days')


class LiveOrderBalanceTests(TestCase):
    def test_order_revisions_count_current_order_balance_once(self):
        invoices=[frappe._dict(name=name,parent='P',customer='C',qas_invoice_type='Store Order',source_doctype='Store Order',source_document='ORDER',outstanding_amount=129,due_date='2026-10-10') for name in ('OLD','CURRENT')]
        order=frappe._dict(name='ORDER',invoice='CURRENT',status='Collected')
        fake=SimpleNamespace(get_all=Mock(side_effect=lambda dt,**kw:invoices if dt=='Sales Invoice' else [{'name':'P','customer':'C','parent_name':'Family'}]),get_doc=Mock(return_value=order),db=SimpleNamespace(sql=Mock(return_value=[])))
        with patch.object(report,'frappe',fake),patch('qas_custom.services.school_admin._safe_fields',side_effect=lambda dt,fields:fields),patch('qas_custom.services.order_invoices.financials',return_value={'remaining_amount':80}) as financials,patch.object(followups,'collect_payg_payment_followups',return_value=[]),patch.object(report,'get_datetime_in_timezone',return_value=datetime(2026,10,18)):
            rows=report.collect_family_followups([], 'TERM')
        financials.assert_called_once_with(order)
        self.assertEqual(rows[0]['outstanding_amount'],80)
        self.assertEqual([item['name'] for item in rows[0]['invoices']],['CURRENT'])
        self.assertIn('Dispatched but unpaid',rows[0]['debt_reasons'])


class DeadlineMigrationTests(TestCase):
    def test_patch_initializes_only_missing_setting(self):
        from qas_custom.patches import v2026_10_18_payg_due_days as migration
        db=SimpleNamespace(get_single_value=Mock(return_value=None),set_single_value=Mock())
        with patch.object(migration,'frappe',SimpleNamespace(db=db)):
            migration.execute()
            db.set_single_value.assert_called_once_with('QAS Invoice Settings','payg_due_days',7)
            db.set_single_value.reset_mock();db.get_single_value.return_value=12
            migration.execute();db.set_single_value.assert_not_called()
