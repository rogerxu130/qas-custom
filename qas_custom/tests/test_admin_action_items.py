from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services import admin_action_items as subject


class TestActionItems(TestCase):
    def setUp(self):
        for target, value in (("require_admin", Mock()), ("brisbane_now", Mock(return_value=datetime(2026, 9, 27)))):
            p = patch.object(subject, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(frappe, 'db', Mock())
        self.db = p.start()
        self.addCleanup(p.stop)
        self.db.count.return_value = 65

    def test_denied_access_never_reads_records(self):
        subject.require_admin.side_effect = PermissionError('Denied')
        with patch.object(frappe, 'get_all') as read, self.assertRaises(PermissionError):
            subject.get_action_items()
        read.assert_not_called()
        self.db.count.assert_not_called()

    def test_pagination_preserves_total_and_uses_same_filters_as_count(self):
        with patch.object(frappe, 'get_all', return_value=[{'name': 'I31'}]) as read:
            result = subject.get_action_items('enrollment', start=30)
        self.assertEqual(result['total'], 65)
        self.assertTrue(result['has_more'])
        self.assertEqual(read.call_args.kwargs['limit_start'], 30)
        self.assertEqual(read.call_args.kwargs['limit_page_length'], 30)
        self.assertEqual(read.call_args.kwargs['filters'], self.db.count.call_args_list[0].kwargs['filters'])

    def test_enrollments_are_not_filtered_by_today_or_upcoming_range(self):
        filters = subject.queue_definitions()['enrollment'][1]
        self.assertNotIn('requested_start_date', filters)
        self.assertNotIn('current_appointment_date', filters)
        self.assertEqual(set(filters['status'][1]), {'New', 'Planned', 'Needs Review'})
        self.assertEqual(filters['converted_enrollment'], ['is', 'not set'])

    def test_completed_work_is_excluded(self):
        definitions = subject.queue_definitions()
        self.assertEqual(definitions['draft_invoice'][1]['docstatus'], 0)
        self.assertEqual(definitions['payment_review'][1]['status'], 'Pending Review')
        self.assertNotIn('Collected', definitions['store_order'][1]['status'][1])
        self.assertNotIn('Converted', definitions['follow_up'][1]['status'][1])
        self.assertEqual(definitions['follow_up'][1]['converted_trial_inquiry'], ['is', 'not set'])

    def test_today_is_not_treated_as_missed_attendance(self):
        self.assertEqual(subject.queue_definitions()['attendance'][1]['current_appointment_date'], ['<', '2026-09-27'])

    def test_invalid_category_does_not_read_and_limit_is_bounded(self):
        with patch.object(frappe, 'throw', side_effect=ValueError), self.assertRaises(ValueError):
            subject.get_action_items('secret-doctype')
        self.db.count.assert_not_called()
        with patch.object(frappe, 'get_all', return_value=[]) as read:
            subject.get_action_items('draft_invoice', start=-20, limit=100000)
        self.assertEqual(read.call_args.kwargs['limit_start'], 0)
        self.assertEqual(read.call_args.kwargs['limit_page_length'], 100)

    def test_payment_review_has_source_link_and_does_not_write(self):
        with patch.object(frappe, 'get_all', return_value=[{'name': 'REQ-1'}]), patch.object(subject, 'get_url_to_form', return_value='https://example.com/app/payment-collection-request/REQ-1'):
            result = subject.get_action_items('payment_review')
        self.assertTrue(result['items'][0]['review_url'].endswith('/REQ-1'))
        self.db.set_value.assert_not_called()
        self.db.commit.assert_not_called()

    def test_direct_enrollment_cannot_use_attendance_actions(self):
        from qas_custom.services.inquiry import mark_inquiry_status_core
        for status in ('Completed', 'No-show', 'Follow-up', 'Further Trial Booked'):
            doc = Mock(inquiry_type='Direct Enrollment')
            with self.subTest(status=status), patch.object(frappe, 'get_doc', return_value=doc), patch.object(frappe, '_', side_effect=lambda value: value), patch('qas_custom.services.inquiry._', side_effect=lambda value: value), patch.object(frappe, 'throw', side_effect=ValueError('Direct Enrollment')):
                with self.assertRaisesRegex(ValueError, 'Direct Enrollment'):
                    mark_inquiry_status_core('INQ', status)
            doc.save.assert_not_called()
