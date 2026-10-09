from datetime import datetime, timedelta
from inspect import unwrap
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import inquiry_parking as parking

NOW = datetime(2026, 10, 3, 10)


class FakeInquiry(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def inquiry(**kwargs):
    values = dict(name='INQ-001', status='Parked', student='STU-001',
                  parked_at=NOW - timedelta(days=30), parked_last_activity_at=NOW - timedelta(days=30),
                  parked_previous_status='Completed', converted_enrollment=None, converted_trial_inquiry=None,
                  save=Mock(), get_doc_before_save=Mock(return_value=FakeInquiry(status='Completed')))
    values.update(kwargs)
    return FakeInquiry(**values)


class TestInquiryParking(TestCase):
    def setUp(self):
        self.db = SimpleNamespace(get_single_value=Mock(return_value=30), commit=Mock(), rollback=Mock())
        self.frappe = SimpleNamespace(db=self.db, get_all=Mock(return_value=['INQ-001']), get_doc=Mock(),
                                     log_error=Mock(), get_traceback=Mock(return_value='error'))
        self.frappe.throw = Mock(side_effect=lambda message: (_ for _ in ()).throw(ValueError(message)))
        for target, value in [('frappe', self.frappe), ('_', lambda text: text), ('now_datetime', lambda: NOW)]:
            patcher = patch.object(parking, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_wait_days_default_and_current_global_setting(self):
        self.db.get_single_value.return_value = None
        self.assertEqual(parking.get_wait_days(), 30)
        self.db.get_single_value.return_value = 0
        self.assertEqual(parking.get_wait_days(), 30)
        self.db.get_single_value.return_value = 14
        self.assertEqual(parking.get_wait_days(), 14)

    def test_wait_days_requires_positive_integer(self):
        for value in [0, -1, '', None, True, '1.5', 'abc', 3651]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parking.validate_wait_days(value)
        self.assertEqual(parking.validate_wait_days('30'), 30)

    def test_existing_parked_deadline_uses_live_global_setting(self):
        doc = inquiry()
        self.assertEqual(parking.parking_summary(doc)['closes_at'], str(NOW))
        self.db.get_single_value.return_value = 60
        self.assertEqual(parking.parking_summary(doc)['closes_at'], str(NOW + timedelta(days=30)))
        doc.status = 'Completed'
        self.assertIsNone(parking.parking_summary(doc)['closes_at'])

    def test_lock_uses_current_locked_read(self):
        parking.lock_inquiry('INQ-001')
        self.frappe.get_doc.assert_called_once_with('Inquiry', 'INQ-001', for_update=True)

    def test_entering_parked_starts_new_period_and_remembers_status(self):
        doc = inquiry(parked_at=NOW - timedelta(days=60))
        parking.validate_parking_transition(doc)
        self.assertEqual(doc.parked_at, NOW)
        self.assertEqual(doc.parked_last_activity_at, NOW)
        self.assertEqual(doc.parked_previous_status, 'Completed')

    def test_ordinary_save_does_not_extend_period(self):
        doc = inquiry(get_doc_before_save=Mock(return_value=FakeInquiry(status='Parked')))
        parking.validate_parking_transition(doc)
        self.assertEqual(doc.parked_last_activity_at, NOW - timedelta(days=30))

    def test_final_or_converted_inquiry_cannot_enter_parked(self):
        for status in ['Inactive', 'Converted', 'Cancelled']:
            with self.subTest(status=status), self.assertRaises(ValueError):
                parking.validate_parking_transition(inquiry(get_doc_before_save=Mock(return_value=FakeInquiry(status=status))))
        with self.assertRaises(ValueError):
            parking.validate_parking_transition(inquiry(converted_enrollment='ENR-001'))

    def test_parking_preserves_existing_appointment(self):
        doc = inquiry(current_appointment_date='2026-10-04', current_appointment_time='10:00:00',
                      get_doc_before_save=Mock(return_value=FakeInquiry(status='Booked')))
        parking.validate_parking_transition(doc)
        self.assertEqual(doc.parked_previous_status, 'Booked')
        self.assertEqual(doc.current_appointment_date, '2026-10-04')
        self.assertEqual(doc.current_appointment_time, '10:00:00')

    def run_action(self, doc, action, note=None):
        with patch.object(parking, 'lock_inquiry', return_value=doc), patch.object(parking, 'add_parking_note') as add_note, patch(
            'qas_custom.services.inquiry.build_inquiry_detail', return_value={'inquiry': {'id': doc.name}}
        ):
            parking.change_parking(doc.name, action, note)
        return add_note

    def test_repeated_park_is_idempotent(self):
        doc = inquiry()
        add_note = self.run_action(doc, 'park')
        doc.save.assert_not_called()
        add_note.assert_not_called()
        self.assertEqual(doc.parked_last_activity_at, NOW - timedelta(days=30))

    def test_resume_restores_previous_status(self):
        doc = inquiry(parked_previous_status='No-show')
        add_note = self.run_action(doc, 'resume')
        self.assertEqual(doc.status, 'No-show')
        doc.save.assert_called_once()
        add_note.assert_called_once()

    def test_progress_restarts_timer_with_a_note(self):
        doc = inquiry()
        add_note = self.run_action(doc, 'progress', 'Parent replied and is considering next term')
        self.assertEqual(doc.parked_last_activity_at, NOW)
        add_note.assert_called_once()
        self.assertFalse(add_note.call_args.kwargs['system'])

    def test_progress_requires_nonempty_note_and_parked_status(self):
        for doc, note in [(inquiry(), '  '), (inquiry(status='Inactive'), 'Parent replied')]:
            with self.subTest(status=doc.status), self.assertRaises(ValueError):
                self.run_action(doc, 'progress', note)
            doc.save.assert_not_called()

    def run_scheduler(self, doc):
        with patch.object(parking, 'lock_inquiry', return_value=doc), patch.object(parking, 'add_parking_note') as add_note:
            parking.close_expired_parked_inquiries()
        return add_note

    def test_exact_expiry_closes_and_retains_a_reason(self):
        doc = inquiry()
        add_note = self.run_scheduler(doc)
        self.assertEqual(doc.status, 'Inactive')
        self.assertIn('30 days', doc.inactive_reason)
        doc.save.assert_called_once()
        add_note.assert_called_once()
        self.db.commit.assert_called_once()

    def test_recent_progress_is_rechecked_after_lock(self):
        doc = inquiry(parked_last_activity_at=NOW - timedelta(days=29, hours=23, minutes=59))
        self.run_scheduler(doc)
        doc.save.assert_not_called()
        self.assertEqual(doc.status, 'Parked')

    def test_resume_or_conversion_racing_with_scheduler_is_not_closed(self):
        for doc in [inquiry(status='Completed'), inquiry(status='Converted'), inquiry(converted_enrollment='ENR-001'), inquiry(converted_trial_inquiry='INQ-002')]:
            with self.subTest(status=doc.status):
                self.run_scheduler(doc)
                doc.save.assert_not_called()

    def test_extended_global_period_prevents_old_deadline_closure(self):
        self.db.get_single_value.return_value = 60
        doc = inquiry()
        self.run_scheduler(doc)
        doc.save.assert_not_called()

    def test_shortened_global_period_applies_to_existing_record(self):
        self.db.get_single_value.return_value = 14
        doc = inquiry(parked_last_activity_at=NOW - timedelta(days=15))
        self.run_scheduler(doc)
        self.assertEqual(doc.status, 'Inactive')
        self.assertIn('14 days', doc.inactive_reason)

    def test_repeat_scheduler_does_not_duplicate_closure(self):
        doc = inquiry()
        self.run_scheduler(doc)
        self.run_scheduler(doc)
        doc.save.assert_called_once()

    def test_bad_record_does_not_stop_other_records(self):
        self.frappe.get_all.return_value = ['broken', 'INQ-001']
        doc = inquiry()
        with patch.object(parking, 'lock_inquiry', side_effect=[RuntimeError('failed'), doc]), patch.object(parking, 'add_parking_note'):
            parking.close_expired_parked_inquiries()
        self.assertEqual(doc.status, 'Inactive')
        self.db.rollback.assert_called_once()
        self.frappe.log_error.assert_called_once()

    def test_school_and_campus_api_authorize_before_mutating(self):
        from qas_custom.api import inquiry_parking as api
        with patch.object(api, 'require_school_admin', side_effect=PermissionError), patch.object(api, 'change_parking') as change:
            with self.assertRaises(PermissionError):
                unwrap(api.school_admin_change_parking)('INQ-001', 'park')
            change.assert_not_called()
        with patch('qas_custom.services.campus_admin.reject_support_view_write'), patch(
            'qas_custom.services.campus_admin._require_inquiry_access', side_effect=PermissionError
        ), patch.object(api, 'change_parking') as change:
            with self.assertRaises(PermissionError):
                unwrap(api.campus_admin_change_parking)('INQ-001', 'park')
            change.assert_not_called()

    def test_parked_status_can_be_combined_with_time_queues(self):
        from frappe.utils import getdate
        from qas_custom.services.campus_admin import _campus_admin_inquiry_queue_filters
        for queue in ['upcoming', 'post_trial']:
            filters, _ = _campus_admin_inquiry_queue_filters(queue, status='Parked', reference_date='2026-10-03')
            self.assertEqual(filters, {'current_appointment_date': ['>=' if queue == 'upcoming' else '<', getdate('2026-10-03')]})
        filters, _ = _campus_admin_inquiry_queue_filters('parked', reference_date='2026-10-03')
        self.assertEqual(filters, {'status': 'Parked'})

    def test_restoring_followup_or_closing_preserves_unchanged_session(self):
        from qas_custom.services.inquiry import sync_inquiry_course_session
        for status in ['Parked', 'Follow-up', 'Inactive', 'Needs Review']:
            doc = inquiry(status=status, inquiry_type='Trial Lesson', course_session='SESSION-001',
                          is_new=Mock(return_value=False),
                          get_doc_before_save=Mock(return_value=FakeInquiry(status='Parked', course_session='SESSION-001')))
            with self.subTest(status=status), patch('qas_custom.services.inquiry._get_session_context') as get_session:
                sync_inquiry_course_session(doc)
                self.assertEqual(doc.status, status)
                get_session.assert_not_called()
                self.assertEqual(doc.course_session, 'SESSION-001')

    def test_cancelling_parked_inquiry_still_cancels_attendance(self):
        from qas_custom.services.inquiry import sync_inquiry_course_session
        doc = inquiry(status='Cancelled', inquiry_type='Trial Lesson', course_session='SESSION-001',
                      is_new=Mock(return_value=False),
                      get_doc_before_save=Mock(return_value=FakeInquiry(status='Parked', course_session='SESSION-001')))
        with patch('qas_custom.services.inquiry.cancel_trial_inquiry_attendance_entries') as cancel:
            sync_inquiry_course_session(doc)
            cancel.assert_called_once_with(doc.name)

    def test_school_parked_queue_ignores_appointment_date_and_uses_exact_status(self):
        from qas_custom.services.school_admin import get_school_admin_inquiries_data
        fake = SimpleNamespace(get_all=Mock(side_effect=[[{'total': 0}], []]))
        with patch('qas_custom.services.school_admin._require_school_admin'), patch(
            'qas_custom.services.school_admin._safe_fields', side_effect=lambda doctype, fields: fields
        ), patch('qas_custom.services.school_admin.frappe', fake):
            result = get_school_admin_inquiries_data(queue='parked')
        self.assertEqual(result['total'], 0)
        for call in fake.get_all.call_args_list:
            self.assertEqual(call.kwargs['filters'], {'status': 'Parked'})

    def test_school_parked_queue_rejects_conflicting_status(self):
        from qas_custom.services.school_admin import get_school_admin_inquiries_data
        with patch('qas_custom.services.school_admin._require_school_admin'):
            result = get_school_admin_inquiries_data(queue='parked', status='Completed')
        self.assertEqual(result['items'], [])

    def test_support_view_cannot_mutate_parking(self):
        from qas_custom.api import inquiry_parking as api
        with patch('qas_custom.services.campus_admin.reject_support_view_write', side_effect=PermissionError), patch.object(api, 'change_parking') as change:
            with self.assertRaises(PermissionError):
                unwrap(api.campus_admin_change_parking)('INQ-001', 'park')
            change.assert_not_called()

    def test_existing_planned_status_can_be_parked_and_restored(self):
        doc = inquiry(get_doc_before_save=Mock(return_value=FakeInquiry(status='Planned')))
        parking.validate_parking_transition(doc)
        self.assertEqual(doc.parked_previous_status, 'Planned')
        self.run_action(doc, 'resume')
        self.assertEqual(doc.status, 'Planned')
