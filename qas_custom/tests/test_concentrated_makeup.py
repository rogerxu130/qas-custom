from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import concentrated_makeup as service


class TestConcentratedMakeup(TestCase):
    def test_quota_and_classroom_both_limit_places(self):
        rows = [dict(student='A', enrollment_type='Makeup'), dict(student='B', enrollment_type='Full Term')]
        self.assertEqual(service.remaining_places(rows, 3, 2), 0)
        self.assertEqual(service.remaining_places(rows, 1, 20), 0)
        self.assertEqual(service.remaining_places(rows, 3, 4), 2)
        self.assertEqual(service.remaining_places(rows + [rows[0]], 3, 4), 2)
        self.assertEqual(service.remaining_places(rows, 0, 0), 0)

    def test_adjacent_classes_allowed_but_overlap_blocked(self):
        self.assertFalse(service.overlaps('10:00', '11:00', '11:00', '12:00'))
        self.assertFalse(service.overlaps('11:00', '12:00', '10:00', '11:00'))
        self.assertTrue(service.overlaps('10:00', '11:00', '10:30', '11:30'))
        self.assertTrue(service.overlaps('10:00', '12:00', '10:30', '11:00'))

    @patch.object(service, 'now_datetime', return_value=datetime(2026, 9, 7, 10, 0))
    def test_started_and_completed_sessions_are_unavailable(self, _now):
        session = frappe._dict(status='Scheduled', session_date='2026-09-07')
        self.assertFalse(service.session_is_future(session, frappe._dict(start_time='10:00')))
        self.assertTrue(service.session_is_future(session, frappe._dict(start_time='10:01')))
        session.status = 'Completed'
        self.assertFalse(service.session_is_future(session, frappe._dict(start_time='10:01')))

    def test_lock_order_is_student_voucher_session(self):
        fake = SimpleNamespace(db=SimpleNamespace(sql=Mock()))
        with patch.object(service, 'frappe', fake):
            service.lock_booking('S', 'V', 'C')
        self.assertEqual([c.args[1] for c in fake.db.sql.call_args_list], [('S',), ('V',), ('C',)])
        self.assertTrue(all('FOR UPDATE' in c.args[0] for c in fake.db.sql.call_args_list))

    def guard(self, *, enabled=1, quota=2, rows=None, capacity=10, conflict=False):
        session = frappe._dict(name='CS', concentrated_makeup_enabled=enabled, concentrated_makeup_capacity=quota)
        fake = SimpleNamespace(db=SimpleNamespace(sql=Mock()), throw=Mock(side_effect=ValueError))
        patches = [patch.object(service, 'frappe', fake), patch.object(service, 'session_context', return_value=(session, frappe._dict())), patch.object(service, 'session_is_future', return_value=True), patch.object(service, 'active_rows', return_value=rows or []), patch.object(service, 'student_has_conflict', return_value=conflict), patch.object(service, 'classroom_capacity', return_value=capacity)]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            service.validate_new_place('S', 'CS')

    def test_two_sequential_bookings_fill_quota(self):
        self.guard(rows=[])
        self.guard(rows=[frappe._dict(student='A', enrollment_type='Makeup')])
        with self.assertRaises(ValueError):
            self.guard(rows=[frappe._dict(student=s, enrollment_type='Makeup') for s in ['A','B']])

    def test_closed_full_conflicting_and_unconfigured_classrooms_rejected(self):
        for kwargs in [dict(enabled=0), dict(capacity=0), dict(conflict=True), dict(capacity=1, rows=[frappe._dict(student='A', enrollment_type='Full Term')])]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.guard(**kwargs)

    def test_marking_existing_attendance_does_not_consume_a_place(self):
        before = frappe._dict(status='To be started', course_session='CS', student='S', enrollment_type='Makeup')
        doc = Mock(get_doc_before_save=Mock(return_value=before))
        doc.get.side_effect = lambda key: 'Present' if key == 'status' else before.get(key)
        with patch.object(service, 'validate_new_place') as guard:
            service.validate_attendance(doc)
        guard.assert_not_called()

    def test_restoring_leave_rechecks_capacity(self):
        before = frappe._dict(status='Leave', course_session='CS', student='S', enrollment_type='Full Term')
        doc = Mock(name='ATT', course_session='CS', student='S', enrollment_type='Full Term', get_doc_before_save=Mock(return_value=before))
        doc.get.side_effect = lambda key: 'To be started' if key == 'status' else before.get(key)
        with patch.object(service, 'validate_new_place') as guard:
            service.validate_attendance(doc)
        guard.assert_called_once()

    def test_restored_regular_row_with_voucher_consumes_makeup_quota(self):
        rows = [dict(student='A', enrollment_type='Full Term', makeup_voucher='MV-1')]
        self.assertEqual(service.remaining_places(rows, 1, 20), 0)
        rows = [dict(student='A', enrollment_type='Regular', source_doctype='Makeup Voucher')]
        self.assertEqual(service.remaining_places(rows, 1, 20), 0)

    def test_same_course_voucher_still_requires_explicit_acceptance(self):
        course = frappe._dict(is_makeup_course=1, accepted_makeup_course=[frappe._dict(course='Other')])
        with patch.object(service, 'session_context', return_value=(frappe._dict(), frappe._dict(course='Makeup'))), patch.object(service.frappe, 'get_doc', return_value=course):
            self.assertFalse(service.accepts_voucher('CS', 'Makeup'))
            self.assertTrue(service.accepts_voucher('CS', 'Other'))
            course.is_makeup_course = 0
            self.assertFalse(service.accepts_voucher('CS', 'Other'))

    def test_restored_voucher_linked_regular_row_uses_makeup_guard(self):
        data = frappe._dict(status='To be started', course_session='CS', student='S', enrollment_type='Full Term', makeup_voucher='MV')
        doc = Mock(course_session='CS', student='S', enrollment_type='Full Term')
        doc.get.side_effect=data.get
        doc.get_doc_before_save.return_value=frappe._dict(status='Leave')
        with patch.object(service, 'validate_new_place') as guard:
            service.validate_attendance(doc)
        self.assertEqual(guard.call_args.args[2], 'Makeup')

    def test_locked_latest_voucher_makes_retry_idempotent(self):
        from qas_custom.modules.makeup import commands
        from contextlib import ExitStack
        old = frappe._dict(name='MV', status='Valid', student='S')
        latest = frappe._dict(name='MV', status='Used', student='S', used_on_session='CS')
        fake = SimpleNamespace(get_doc=Mock(return_value=latest), db=SimpleNamespace(sql=Mock(return_value=[frappe._dict(name='ATT')])))
        with ExitStack() as stack:
            stack.enter_context(patch.object(commands, 'frappe', fake))
            stack.enter_context(patch.object(commands, '_get_parent_makeup_voucher', return_value=old))
            stack.enter_context(patch.object(commands, '_get_redeem_student', return_value='S'))
            stack.enter_context(patch.object(commands, '_get_voucher_used_by_student', return_value='S'))
            stack.enter_context(patch.object(commands, '_build_makeup_voucher_payload', return_value={}))
            stack.enter_context(patch.object(commands, '_build_redeem_session_payload', return_value={}))
            lock = stack.enter_context(patch.object(service, 'lock_booking'))
            queue = stack.enter_context(patch.object(commands, 'queue_makeup_booking_confirmation'))
            create = stack.enter_context(patch.object(commands, 'create_makeup_attendance_entry'))
            result = commands.redeem_parent_voucher_core(SimpleNamespace(name='P'), [], 'MV', 'CS', 'S')
        lock.assert_called_once_with('S', 'MV', 'CS')
        fake.get_doc.assert_called_once_with('Makeup Voucher', 'MV', for_update=True)
        self.assertFalse(result['booking_created'])
        create.assert_not_called()
        queue.assert_not_called()

    def test_full_session_failure_precedes_attendance_and_voucher_write(self):
        from qas_custom.modules.makeup import commands
        from contextlib import ExitStack
        voucher=Mock(name='voucher')
        voucher.name='MV'
        voucher.course='Art'
        voucher.get.side_effect=lambda key: 'Valid' if key=='status' else None
        with ExitStack() as stack:
            stack.enter_context(patch.object(commands, 'frappe', SimpleNamespace(get_doc=Mock(return_value=voucher),db=SimpleNamespace(sql=Mock(return_value=[])))))
            for name, value in [('_get_parent_makeup_voucher', voucher), ('_get_redeem_student','S'), ('_validate_voucher_available_for_redeem',None), ('_validate_session_can_redeem_voucher',None), ('_get_reusable_attendance_row_for_voucher',None)]:
                stack.enter_context(patch.object(commands,name,return_value=value))
            stack.enter_context(patch.object(service,'lock_booking'))
            stack.enter_context(patch.object(service,'validate_voucher_target'))
            stack.enter_context(patch.object(service,'validate_new_place',side_effect=ValueError('full')))
            create=stack.enter_context(patch.object(commands,'redeem_voucher_attendance_entry'))
            with self.assertRaises(ValueError):
                commands.redeem_parent_voucher_core(SimpleNamespace(name='P'),[],'MV','CS','S')
        create.assert_not_called()
        voucher.save.assert_not_called()
