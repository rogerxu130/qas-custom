from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import school_admin as admin
from qas_custom.services import enrollment_billing_status as billing
from qas_custom.modules.billing.invoice_settings import apply_course_invoice_dates


class Doc(frappe._dict):
    def __init__(self, **values):
        super().__init__(values)
        self.flags = SimpleNamespace(qas_was_created=False)
        self.save = Mock()
        self.insert = Mock()
    def append(self, key, values):
        row = frappe._dict(values)
        self.setdefault(key, []).append(row)
        return row


def fail(message):
    raise ValueError(message)


def enrollment(**values):
    return Doc(**dict(dict(name='E', status='Planned', enrollment_type='Full-Term', student='S', parent='P',
                          course='C', weekly_timeslot='W', term='T', start_course_session=None), **values))


class AdvanceTermTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("frappe.get_system_settings", return_value=None))
        self.stack.enter_context(patch.object(admin, '_', side_effect=lambda text: text))
        self.stack.enter_context(patch.object(admin, '_set_if_field', side_effect=lambda doc, key, value: doc.update({key: value})))
        self.frappe = self.stack.enter_context(patch.object(admin, 'frappe'))
        self.frappe.throw.side_effect = fail
        self.term = frappe._dict(name='T', status='Upcoming', start_date='2026-10-05', end_date='2026-12-05')
        self.stack.enter_context(patch.object(admin, 'require_open_term', return_value=None))
        self.stack.enter_context(patch.object(admin, 'open_enrollment_or_filters', return_value=[["term", "in", ["T"]]]))
        self.frappe.get_doc.return_value = self.term
        self.frappe.db.get_value.return_value = frappe._dict(name='W', term='T', course='C')
        self.frappe.get_all.return_value = []

    def test_planned_without_sessions_needs_no_attendance_or_start(self):
        doc = enrollment()
        with patch.object(admin, '_enrollment_has_attendance') as attendance:
            self.assertIsNone(admin._enrollment_invoice_start(doc))
        attendance.assert_not_called()
        self.assertEqual(doc.status, 'Planned')

    def test_active_without_sessions_uses_full_term(self):
        self.assertIsNone(admin._enrollment_invoice_start(enrollment(status='Active')))

    def test_missing_attendance_with_first_session_uses_full_term(self):
        self.frappe.get_all.return_value = [frappe._dict(name='CS1', session_date='2026-10-05')]
        with patch.object(admin, '_enrollment_has_attendance', return_value=False):
            self.assertIsNone(admin._enrollment_invoice_start(enrollment(status='Active')))

    def test_normal_active_attendance_retains_session_based_billing(self):
        self.frappe.get_all.return_value = [frappe._dict(name='CS1', session_date='2026-10-05')]
        with patch.object(admin, '_enrollment_has_attendance', return_value=True):
            self.assertEqual(admin._enrollment_invoice_start(enrollment(status='Active')), 'CS1')

    def test_explicit_mid_term_start_still_prorates_before_attendance(self):
        self.frappe.get_all.return_value = [frappe._dict(name='CS1', session_date='2026-10-05')]
        self.frappe.db.get_value.side_effect = [frappe._dict(name='W', term='T', course='C'), '2026-10-19']
        with patch.object(admin, '_validate_enrollment_start_session', return_value='CS3'):
            self.assertEqual(admin._enrollment_invoice_start(enrollment(start_course_session='CS3')), 'CS3')

    def test_invalid_explicit_start_cannot_fall_back_to_full_fee(self):
        with patch.object(admin, '_validate_enrollment_start_session', side_effect=ValueError('cancelled')):
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                admin._enrollment_invoice_start(enrollment(start_course_session='BAD'))

    def test_wrong_term_timeslot_and_course_rejected(self):
        for values in [dict(term='OTHER', course='C'), dict(term='T', course='OTHER')]:
            self.frappe.db.get_value.return_value = frappe._dict(name='W', **values)
            with self.assertRaises(ValueError):
                admin._enrollment_invoice_start(enrollment())

    def test_closed_term_inactive_or_non_full_term_rejected(self):
        for values in [dict(status='Cancelled'), dict(enrollment_type='Trial')]:
            with self.assertRaises(ValueError):
                admin._enrollment_invoice_start(enrollment(**values))
        self.term.status = 'Completed'
        with self.assertRaises(ValueError):
            admin._enrollment_invoice_start(enrollment())

    def invoice_setup(self, *, existing=False):
        invoice = Doc(name='INV', docstatus=0, posting_date='2026-12-29', due_date='2027-02-01',
                      items=[], payment_schedule=[frappe._dict(due_date='2027-02-01')], grand_total=360)
        self.frappe.new_doc.return_value = invoice
        self.frappe.get_doc.return_value = invoice
        for name, result in [('_has_field', True), ('get_invoice_customer', 'CUSTOMER'), ('get_invoice_item', 'ITEM'),
                             ('get_course_money', 360), ('get_course_number', 12),
                             ('_find_draft_family_invoice', 'INV' if existing else None), ('_invoice_has_enrollment_item', False),
                             ('get_student_parent_name', 'Student'), ('get_student_display_code', 'S'),
                             ('invoice_item_schedule', 'Saturday'), ('build_course_invoice_description', 'Student / Term / 9 sessions')]:
            self.stack.enter_context(patch.object(admin, name, return_value=result))
        for name in ['disable_sales_invoice_auto_notifications', 'apply_default_invoice_dates', '_sync_invoice_student_summary', 'apply_invoice_payment_snapshot']:
            self.stack.enter_context(patch.object(admin, name))
        self.stack.enter_context(patch.object(admin, '_run_school_admin_invoice_mutation', side_effect=lambda callback: callback()))
        return invoice

    def test_full_fee_ignores_course_divisor_no_session_queries_and_sets_seven_days(self):
        invoice = self.invoice_setup()
        with patch.object(admin, '_course_session_count_for_enrollment') as count:
            result = admin._create_term_enrollment_invoice(enrollment(), None)
        count.assert_not_called()
        self.assertIs(result, invoice)
        self.assertEqual(invoice['items'][0].rate, 360)
        self.assertEqual(invoice['items'][0].session_count, 9)
        self.assertIsNone(invoice['items'][0].course_session)
        self.assertIn('Full term', invoice['items'][0].description)
        self.assertEqual(str(invoice.due_date), '2027-01-05')
        self.assertEqual(invoice.payment_schedule[0].due_date, invoice.due_date)
        self.assertEqual(invoice.qas_advance_term_invoice, 1)
        invoice.insert.assert_called_once()

    def test_reused_family_draft_preserves_custom_deadline(self):
        invoice = self.invoice_setup(existing=True)
        admin._create_term_enrollment_invoice(enrollment(), None)
        self.assertEqual(invoice.due_date, '2027-02-01')
        self.assertEqual(invoice.payment_schedule[0].due_date, '2027-02-01')
        invoice.save.assert_called_once()
        invoice.insert.assert_not_called()

    def test_later_date_calculation_does_not_overwrite_manual_deadline(self):
        invoice = Doc(docstatus=0, qas_advance_term_invoice=1, due_date='2027-02-09')
        apply_course_invoice_dates(invoice, enrollment=enrollment(status='Active', start_course_session='CS'))
        self.assertEqual(invoice.due_date, '2027-02-09')

    def test_missing_migration_or_fee_rejects_without_save(self):
        invoice = self.invoice_setup()
        for name, result in [('_has_field', False), ('get_course_money', 0)]:
            with patch.object(admin, name, return_value=result):
                with self.assertRaises(ValueError):
                    admin._create_term_enrollment_invoice(enrollment(), None)
        invoice.insert.assert_not_called()

    def single_setup(self, doc):
        self.frappe.get_doc.return_value = doc
        for name in ['_require_school_admin', '_add_comment']:
            self.stack.enter_context(patch.object(admin, name))
        self.stack.enter_context(patch.object(admin, '_get_payload', side_effect=lambda payload: payload or {}))
        self.stack.enter_context(patch.object(admin, '_build_enrollment_payload', side_effect=lambda value: value))

    def test_single_invoice_keeps_planned_and_creates_no_attendance(self):
        doc = enrollment()
        self.single_setup(doc)
        with patch.object(admin, '_existing_invoice_for_enrollment', return_value=None), patch.object(admin, '_enrollment_invoice_start', return_value=None), patch.object(admin, '_create_term_enrollment_invoice', return_value=Doc(name='INV', grand_total=360)), patch.object(admin, '_create_enrollment_attendance_entries') as attend:
            result = admin.create_school_admin_enrollment_invoice_data('E')
        self.assertEqual(result['invoice'], 'INV')
        self.assertEqual(doc.status, 'Planned')
        self.assertIsNone(doc.start_course_session)
        attend.assert_not_called()
        doc.save.assert_called_once()

    def test_single_duplicate_invoice_blocked(self):
        self.single_setup(enrollment(invoice='INV'))
        with patch.object(admin, '_existing_invoice_for_enrollment', return_value='INV'), patch.object(admin, '_create_term_enrollment_invoice') as create:
            with self.assertRaisesRegex(ValueError, 'already has'):
                admin.create_school_admin_enrollment_invoice_data('E')
        create.assert_not_called()

    def test_batch_includes_planned_skips_existing_and_leaves_state(self):
        doc = enrollment()
        self.frappe.get_doc.return_value = doc
        with patch.object(admin, '_existing_invoice_for_enrollment', side_effect=[None, 'INV']), patch.object(admin, '_enrollment_invoice_start', return_value=None), patch.object(admin, '_create_term_enrollment_invoice', return_value=Doc(name='INV', grand_total=360)) as create, patch.object(admin, '_add_comment'):
            result = admin._create_invoices_for_enrollment_names(['E', 'E'])
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['invoice_items'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(doc.status, 'Planned')
        create.assert_called_once()

    def test_candidate_query_includes_both_states(self):
        with patch.object(admin, '_doctype_available', return_value=True):
            admin._get_invoice_candidate_enrollment_names(term='T')
        self.assertEqual(self.frappe.get_all.call_args.kwargs['filters']['status'], ['in', ['Planned', 'Active']])

    def activation_setup(self):
        self.frappe.db.get_value.side_effect = [frappe._dict(name='W', term='T', course='C'), '2026-10-05']
        for name, value in [('_safe_fields', []), ('_duplicate_active_enrollment', None), ('_first_course_session_for_timeslot', 'CS'), ('_validate_enrollment_start_session', 'CS')]:
            self.stack.enter_context(patch.object(admin, name, return_value=value))
        self.stack.enter_context(patch.object(admin, '_add_comment'))

    def test_activation_retains_paid_invoice_and_never_creates_another(self):
        doc = enrollment(invoice='PAID', invoice_status='Paid', invoice_amount=360)
        self.activation_setup()
        with patch.object(admin, '_create_enrollment_attendance_entries', return_value=['CS']), patch.object(admin, '_create_term_enrollment_invoice') as create:
            admin._activate_planned_enrollment(doc, self.term)
        self.assertEqual((doc.status, doc.invoice, doc.invoice_status, doc.invoice_amount), ('Active', 'PAID', 'Paid', 360))
        doc.save.assert_called_once()
        create.assert_not_called()

    def test_failed_attendance_never_persists_active(self):
        for result in [[], ValueError('attendance error')]:
            with self.subTest(result=result):
                doc = enrollment(invoice='PAID')
                self.activation_setup()
                with patch.object(admin, '_create_enrollment_attendance_entries', **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                    with self.assertRaises(ValueError):
                        admin._activate_planned_enrollment(doc, self.term)
                doc.save.assert_not_called()
                self.assertEqual(doc.invoice, 'PAID')

    def test_planned_billed_enrollment_cannot_be_cancelled_or_changed_directly(self):
        doc = enrollment(invoice='INV')
        self.single_setup(doc)
        with patch.object(admin, 'today', return_value='2026-10-01'), patch.object(admin, '_existing_invoice_for_enrollment', return_value='INV'):
            with self.assertRaisesRegex(ValueError, 'invoice'):
                admin.end_school_admin_enrollment_data('E')
            with self.assertRaisesRegex(ValueError, 'invoice'):
                admin.update_school_admin_enrollment_data('E', {'weekly_timeslot': 'OTHER'})
        doc.save.assert_not_called()


    def test_normal_session_pricing_still_uses_remaining_count(self):
        invoice = self.invoice_setup()
        with patch.object(admin, '_course_session_count_for_enrollment', return_value=4), patch.object(admin, 'get_course_session_snapshot_label', return_value='CS3'), patch.object(admin, 'apply_course_invoice_dates') as dates:
            admin._create_term_enrollment_invoice(enrollment(status='Active'), 'CS3')
        self.assertEqual(invoice['items'][0].rate, 120)
        self.assertEqual(invoice['items'][0].session_count, 4)
        self.assertFalse(invoice.get('qas_advance_term_invoice'))
        dates.assert_called_once()

    def test_repeat_attendance_keeps_existing_entry(self):
        doc = enrollment(status='Active', invoice='INV')
        self.frappe.db.get_value.side_effect = [None, frappe._dict(name='ATT', status='To be started')]
        with patch.object(admin, 'create_attendance_entry', return_value='ATT') as create:
            first = admin._ensure_enrollment_attendance_entries(doc, [frappe._dict(name='CS')])
            second = admin._ensure_enrollment_attendance_entries(doc, [frappe._dict(name='CS')])
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['retained'], 1)
        create.assert_called_once()
        self.assertEqual(doc.invoice, 'INV')

    def test_term_roster_does_not_hide_records_after_five_hundred(self):
        roster = [dict(name=f'E{i}', status='Planned', billing_state='Paid', awaiting_attendance=True) for i in range(601)]
        def get_rows(*, filters, limit):
            return roster[:limit] if limit else roster
        with patch.object(admin, '_require_school_admin'), patch.object(admin, '_document_payload', return_value=dict(self.term)), patch.object(admin, '_count', return_value=601), patch.object(admin, '_get_enrollment_rows', side_effect=get_rows), patch.object(admin, 'get_school_admin_weekly_timeslots_data', return_value={'items': []}), patch.object(admin, '_has_field', return_value=False), patch.object(admin, '_get_weekly_timeslot_reference_options', return_value={}):
            result = admin.get_school_admin_term_data('T')
        self.assertEqual(len(result['planned_enrollments']), 601)
        self.assertEqual(result['planned_enrollments'][-1]['name'], 'E600')


class BillingStatusTests(TestCase):
    def test_actual_invoice_states(self):
        for status, total, outstanding, expected in [(0, 360, 0, 'Draft'), (1, 360, 360, 'Unpaid'), (1, 360, 100, 'Partly Paid'), (1, 360, 0, 'Paid'), (2, 360, 0, 'Not invoiced')]:
            self.assertEqual(billing.billing_state(dict(docstatus=status, grand_total=total, outstanding_amount=outstanding)), expected)

    def test_amended_invoice_overrides_cancelled_pointer_and_snapshot(self):
        rows = [dict(name='E', status='Planned', invoice='OLD', invoice_status='Paid')]
        with patch.object(billing.frappe, 'get_all', side_effect=[
            [frappe._dict(enrollment='E', parent='NEW')],
            [frappe._dict(name='NEW', docstatus=0, status='Draft', grand_total=360, outstanding_amount=360)]
        ]):
            billing.attach_billing_status(rows)
        self.assertEqual(rows[0]['billing_state'], 'Draft')
        self.assertEqual(rows[0]['billing_invoice'], 'NEW')
        self.assertTrue(rows[0]['awaiting_attendance'])

    def test_cancelled_invoice_no_longer_counts_as_billed(self):
        rows = [dict(name='E', status='Planned', invoice='OLD', invoice_status='Paid')]
        with patch.object(billing.frappe, 'get_all', side_effect=[[], []]):
            billing.attach_billing_status(rows)
        self.assertEqual(rows[0]['billing_state'], 'Not invoiced')
        self.assertFalse(rows[0]['awaiting_attendance'])

    def test_paid_active_enrollment_is_not_pending(self):
        rows = [dict(name='E', status='Active', invoice='I')]
        with patch.object(billing.frappe, 'get_all', side_effect=[[], [frappe._dict(name='I', docstatus=1, outstanding_amount=0, grand_total=360)]]):
            billing.attach_billing_status(rows)
        self.assertEqual(rows[0]['billing_state'], 'Paid')
        self.assertFalse(rows[0]['awaiting_attendance'])
