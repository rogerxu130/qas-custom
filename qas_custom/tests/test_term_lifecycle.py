from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import term_lifecycle as service


class TermLifecycleTests(TestCase):
    def setUp(self):
        previous_flags = getattr(frappe.local, 'flags', None)
        frappe.local.flags = frappe._dict(in_test=False)
        def restore_flags():
            if previous_flags is None:
                del frappe.local.flags
            else:
                frappe.local.flags = previous_flags
        self.addCleanup(restore_flags)
        patcher = patch.object(frappe, 'db', Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def row(self, day='2026-09-15', end='16:00:00', status='Scheduled'):
        return frappe._dict(session_date=day, end_time=end, status=status)

    def test_future_and_today_unfinished_block(self):
        now = datetime(2026, 9, 15, 15)
        for row in [self.row('2026-09-16'), self.row(), self.row(end=None), self.row(day=None)]:
            with self.subTest(row=row):
                self.assertTrue(service._unfinished_session(row, now))

    def test_past_finished_and_cancelled_do_not_block(self):
        now = datetime(2026, 9, 15, 16)
        for row in [self.row('2026-09-14'), self.row(), self.row('2026-09-16', status='Cancelled'), self.row(status='Completed')]:
            with self.subTest(row=row):
                self.assertFalse(service._unfinished_session(row, now))

    def term(self, status='Active', prior=None):
        doc = frappe._dict(name='Term 3', status=status, closed_from_status=prior, flags=frappe._dict())
        doc.get_doc_before_save = Mock(return_value=frappe._dict(status=status))
        doc.save = Mock()
        doc.add_comment = Mock()
        return doc

    def run_transition(self, doc, action='close', confirmed=1, blockers=False, fail=False, reopen=None):
        db = Mock()
        fake = SimpleNamespace(db=db, session=SimpleNamespace(user='admin'),
                               get_doc=Mock(return_value=doc), get_meta=Mock(return_value=SimpleNamespace(has_field=lambda name: True)), throw=Mock(side_effect=ValueError))
        if fail:
            doc.save.side_effect = ValueError('save failed')
        with patch.object(service, 'frappe', fake), patch.object(service, '_', side_effect=lambda s: s), \
             patch.object(service, 'lock_term') as lock, \
             patch.object(service, 'closure_preview', return_value={'can_close': not blockers, 'blockers': ['future']}) as preview, \
             patch.object(service, 'now_datetime', return_value=datetime(2026, 9, 15, 17)), \
             patch('qas_custom.services.school_admin._require_school_admin'):
            try:
                result = service.transition('Term 3', action, confirmed, reopen)
            except ValueError:
                result = None
        return result, fake, lock, preview

    def test_close_only_writes_term_and_records_previous_state(self):
        doc = self.term('Upcoming')
        result, fake, lock, preview = self.run_transition(doc)
        self.assertEqual(result['status'], 'Archived')
        self.assertEqual(doc.closed_from_status, 'Upcoming')
        fake.get_doc.assert_called_once_with('Term', 'Term 3', for_update=True)
        doc.save.assert_called_once_with(ignore_permissions=True)
        fake.db.set_value.assert_not_called()
        fake.db.delete.assert_not_called()
        preview.assert_called_once_with('Term 3', for_update=True)
        lock.assert_called_once_with('Term 3')

    def test_confirmation_is_required_before_writes(self):
        doc = self.term()
        result, fake, lock, _ = self.run_transition(doc, confirmed=0)
        self.assertIsNone(result)
        lock.assert_not_called()
        doc.save.assert_not_called()

    def test_future_classes_rechecked_and_block_save(self):
        doc = self.term()
        result, fake, _, _ = self.run_transition(doc, blockers=True)
        self.assertIsNone(result)
        self.assertEqual(doc.status, 'Active')
        doc.save.assert_not_called()
        fake.db.rollback.assert_called_once_with(save_point='term_transition')

    def test_failed_close_rolls_back(self):
        result, fake, _, _ = self.run_transition(self.term(), fail=True)
        self.assertIsNone(result)
        fake.db.rollback.assert_called_once_with(save_point='term_transition')

    def test_reopen_restores_only_term(self):
        doc = self.term('Archived', 'Upcoming')
        result, fake, _, preview = self.run_transition(doc, action='reopen')
        self.assertEqual(result['status'], 'Upcoming')
        preview.assert_not_called()
        fake.get_doc.assert_called_once_with('Term', 'Term 3', for_update=True)
        fake.db.set_value.assert_not_called()

    def test_legacy_reopen_requires_explicit_status(self):
        result, _, _, _ = self.run_transition(self.term('Completed'), action='reopen')
        self.assertIsNone(result)
        result, _, _, _ = self.run_transition(self.term('Completed'), action='reopen', reopen='Active')
        self.assertEqual(result['status'], 'Active')

    def test_repeated_transition_does_not_save(self):
        for status, action in [('Archived', 'close'), ('Completed', 'close'), ('Active', 'reopen')]:
            doc = self.term(status)
            result, _, _, _ = self.run_transition(doc, action=action)
            self.assertTrue(result['unchanged'])
            doc.save.assert_not_called()

    def test_generic_save_cannot_close_or_reopen(self):
        for old, new in [('Active', 'Archived'), ('Active', 'Completed'), ('Archived', 'Active')]:
            doc = self.term(old)
            doc.status = new
            with patch.object(service, '_', side_effect=lambda s:s), patch.object(service.frappe, 'throw', side_effect=ValueError):
                with self.assertRaises(ValueError):
                    service.validate_term(doc)

    def test_enrollment_cancellation_and_invoice_updates_remain_allowed(self):
        old = frappe._dict(term='Old', weekly_timeslot='slot', status='Active', enrollment_type='Full-Term')
        doc = frappe._dict(old)
        doc.doctype = 'Enrollment'; doc.status = 'Cancelled'
        doc.get_doc_before_save = Mock(return_value=old)
        with patch.object(service.frappe.db, 'get_value', return_value='Old'), patch.object(service, 'require_open_term') as guard:
            service.validate_term_child(doc)
        guard.assert_not_called()

    def test_enrollment_cannot_move_out_of_closed_term(self):
        old = frappe._dict(term='Old', weekly_timeslot='old-slot', status='Active', enrollment_type='Full-Term')
        doc = frappe._dict(old); doc.term='New'; doc.weekly_timeslot='new-slot'; doc.doctype='Enrollment'
        doc.get_doc_before_save=Mock(return_value=old)
        with patch.object(service.frappe.db, 'get_value', return_value='New'), patch.object(service, 'require_open_term', side_effect=ValueError) as guard:
            with self.assertRaises(ValueError):
                service.validate_term_child(doc)
        guard.assert_called_once_with('Old')

    def test_term_and_timeslot_mismatch_is_rejected(self):
        doc=frappe._dict(doctype='Enrollment',term='New',weekly_timeslot='old-slot',enrollment_type='Full-Term')
        doc.get_doc_before_save=Mock(return_value=None)
        with patch.object(service.frappe.db, 'get_value', return_value='Old'), patch.object(service.frappe, 'throw', side_effect=ValueError), patch.object(service, '_', side_effect=lambda s:s):
            with self.assertRaises(ValueError): service.validate_term_child(doc)

    def test_existing_attendance_correction_does_not_require_open_term(self):
        doc=frappe._dict(doctype='Class Attendance Entry',course_session='session',status='Present')
        doc.get_doc_before_save=Mock(return_value=frappe._dict(course_session='session',status='Absent'))
        with patch.object(service, 'require_open_timeslot') as guard:
            service.validate_term_child(doc)
        guard.assert_not_called()

    def test_new_attendance_checks_term(self):
        doc=frappe._dict(doctype='Class Attendance Entry',course_session='session')
        doc.get_doc_before_save=Mock(return_value=None)
        with patch.object(service.frappe.db, 'get_value', return_value='slot'), patch.object(service, 'require_open_timeslot') as guard:
            service.validate_term_child(doc)
        guard.assert_called_once_with('slot')

    def test_open_scope_preserves_termless_records(self):
        with patch.object(service.frappe, 'get_all', return_value=['Term 4']) as query:
            result=service.open_enrollment_or_filters()
        self.assertEqual(result, [['term','in',['Term 4']], ['term','is','not set']])
        self.assertEqual(query.call_args.kwargs['filters']['status'][1], ('Upcoming','Active'))

    def test_family_history_scopes_before_pagination(self):
        from qas_custom.services import school_admin
        for show_closed in (0, 1):
            with self.subTest(show_closed=show_closed):
                rows = [{'name': str(i), 'term': 'Old'} for i in range(51)]
                with patch.object(school_admin, '_require_school_admin'), \
                     patch.object(school_admin, '_resolve_family_context', return_value={'parent': 'P'}), \
                     patch.object(school_admin, '_get_family_students', return_value=[{'name': 'S'}]), \
                     patch.object(school_admin, '_get_enrollment_rows', return_value=rows) as query, \
                     patch.object(service.frappe, 'get_all', return_value=[frappe._dict(name='Old', status='Archived')]):
                    result = service.family_enrollments(parent='P', include_closed=show_closed, start=50)
                self.assertEqual(len(result['items']), 50)
                self.assertTrue(result['has_more'])
                self.assertTrue(result['items'][0]['term_closed'])
                self.assertEqual(query.call_args.kwargs['start'], 50)
                self.assertEqual(query.call_args.kwargs['limit'], 51)
                self.assertEqual(query.call_args.kwargs['open_terms_only'], not show_closed)
                self.assertEqual(query.call_args.kwargs['filters'], {} if show_closed else {'status': ['in', ['Active', 'Planned']]})

    def test_unresolved_family_never_queries_all_enrollments(self):
        from qas_custom.services import school_admin
        with patch.object(school_admin, '_require_school_admin'), \
             patch.object(school_admin, '_resolve_family_context', return_value={'customer': 'C'}), \
             patch.object(school_admin, '_get_family_students', return_value=[]), \
             patch.object(school_admin, '_get_enrollment_rows') as query:
            result = service.family_enrollments(customer='C')
        self.assertEqual(result['items'], [])
        query.assert_not_called()

    def test_students_with_planned_enrollment_remain_active_in_open_terms(self):
        from qas_custom.services import maintenance
        with patch.object(service.frappe, 'get_all', return_value=['S']) as query, \
             patch.object(service, 'open_enrollment_or_filters', return_value=[['term', 'in', ['New']]]), \
             patch.object(maintenance, '_pluck_students', return_value=[]), \
             patch.object(maintenance, '_get_students_with_future_attendance', return_value=[]), \
             patch.object(maintenance, '_get_students_with_open_course_invoices', return_value=[]):
            result = maintenance._get_students_with_active_business()
        self.assertEqual(result, {'S'})
        self.assertEqual(query.call_args.kwargs['filters'], {'status': ['in', ['Active', 'Planned']]})
        self.assertEqual(query.call_args.kwargs['or_filters'], [['term', 'in', ['New']]])

    def test_closed_term_guard_rejects_write(self):
        with patch.object(service, 'lock_term', return_value=frappe._dict(status='Archived')), \
             patch.object(service.frappe, 'throw', side_effect=ValueError), patch.object(service, '_', side_effect=lambda s:s):
            with self.assertRaises(ValueError): service.require_open_term('Old')

    def test_client_supplied_flags_cannot_bypass_confirmation(self):
        doc = self.term('Active')
        doc.status = 'Archived'
        doc.flags.term_transition_confirmed = True
        with patch.object(service.frappe, 'throw', side_effect=ValueError), patch.object(service, '_', side_effect=lambda s:s):
            with self.assertRaises(ValueError): service.validate_term(doc)

    def test_internal_confirmation_is_limited_to_target_term_and_state(self):
        doc = self.term('Active')
        doc.status = 'Archived'
        token = service._confirmed_transition.set((doc.name, doc.status))
        try:
            service.validate_term(doc)
            other = self.term('Active'); other.name='Other'; other.status='Archived'
            with patch.object(service.frappe, 'throw', side_effect=ValueError), patch.object(service, '_', side_effect=lambda s:s):
                with self.assertRaises(ValueError): service.validate_term(other)
        finally:
            service._confirmed_transition.reset(token)
