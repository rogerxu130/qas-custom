from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.notifications import enrollment_terms as subject
from qas_custom.modules.billing import invoice_settings


class TestTermsNotice(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = self.stack.enter_context(patch.object(frappe, 'db', Mock()))
        self.stack.enter_context(patch.object(frappe, 'log_error'))
        self.stack.enter_context(patch.object(frappe, 'get_traceback', return_value='test error'))
        self.settings = {'school_name': 'QAS', 'school_email': 'school@example.com', 'enrollment_terms': 'Terms v1\nRefund policy <text>'}
        self.stack.enter_context(patch.object(subject, 'get_invoice_settings', return_value=self.settings))
        self.stack.enter_context(patch.object(subject, '_notification_log_available', return_value=True))
        self.stack.enter_context(patch.object(frappe, 'get_meta', return_value=Mock(has_field=Mock(return_value=True))))
        self.db.get_value.return_value = None
        self.stack.enter_context(patch.object(subject, '_invoice_recipient', return_value={'email':'parent@example.com'}))
        self.create_log = self.stack.enter_context(patch.object(subject, '_create_notification_log', return_value='LOG'))
        self.queued = self.stack.enter_context(patch.object(subject, '_mark_notification_queued'))
        self.failed = self.stack.enter_context(patch.object(subject, '_mark_notification_failed'))
        self.sent = self.stack.enter_context(patch.object(subject, '_mark_notification_sent'))
        self.enqueue = self.stack.enter_context(patch.object(frappe, 'enqueue'))
        self.send = self.stack.enter_context(patch.object(subject, 'sendmail_or_skip', return_value=None))
        self.enrollment = frappe._dict(name='ENR-1')
        self.invoice = frappe._dict(name='INV-1')

    def test_blank_terms_do_not_send_or_reserve(self):
        self.settings['enrollment_terms'] = ' \n '
        self.assertTrue(subject.queue_enrollment_terms_notice(self.enrollment, self.invoice)['skipped'])
        self.create_log.assert_not_called()
        self.enqueue.assert_not_called()

    def test_snapshot_is_saved_before_after_commit_job(self):
        result = subject.queue_enrollment_terms_notice(self.enrollment, self.invoice)
        self.assertTrue(result['queued'])
        payload = self.create_log.call_args.kwargs
        self.assertIn('Refund policy &lt;text&gt;', payload['message'])
        self.assertIn('please reply to this email', payload['message'])
        self.assertEqual(payload['document_name'], 'ENR-1')
        self.assertTrue(self.enqueue.call_args.kwargs['enqueue_after_commit'])
        self.assertEqual(self.enqueue.call_args.kwargs['reply_to'], 'school@example.com')
        self.send.assert_not_called()
        self.db.commit.assert_not_called()

    def test_repeated_conversion_does_not_create_another_notice(self):
        self.db.get_value.return_value = 'EXISTING'
        result = subject.queue_enrollment_terms_notice(self.enrollment, self.invoice)
        self.assertTrue(result['duplicate'])
        self.create_log.assert_not_called()
        self.enqueue.assert_not_called()

    def test_queue_failure_is_audited_without_raising_into_conversion(self):
        self.enqueue.side_effect = RuntimeError('Queue offline')
        result = subject.queue_enrollment_terms_notice(self.enrollment, self.invoice)
        self.assertFalse(result['queued'])
        self.failed.assert_called_once()

    def test_missing_recipient_or_reply_address_does_not_block_conversion(self):
        self.settings['school_email'] = ''
        self.assertFalse(subject.queue_enrollment_terms_notice(self.enrollment, self.invoice)['queued'])
        self.enqueue.assert_not_called()

    def _log(self, status='Queued'):
        doc = frappe._dict(name='LOG', event_key='enrollment_terms:key', delivery_status=status,
                          email_to='parent@example.com', subject='Terms', email_content='Saved v1 terms', document_name='ENR-1')
        self.stack.enter_context(patch.object(frappe, 'get_doc', return_value=doc))
        return doc

    def test_worker_uses_saved_content_not_changed_settings(self):
        self._log()
        self.settings['enrollment_terms'] = 'New version'
        self.assertTrue(subject.send_enrollment_terms_notice_job('LOG', 'school@example.com')['sent'])
        self.assertEqual(self.send.call_args.kwargs['message'], 'Saved v1 terms')
        self.assertEqual(self.send.call_args.kwargs['reply_to'], 'school@example.com')
        self.sent.assert_called_once_with('LOG')

    def test_sent_notice_does_not_resend(self):
        self._log('Sent')
        self.assertTrue(subject.send_enrollment_terms_notice_job('LOG','school@example.com')['duplicate'])
        self.send.assert_not_called()

    def test_failed_notice_can_retry_same_snapshot(self):
        self._log('Failed')
        self.assertTrue(subject.send_enrollment_terms_notice_job('LOG','school@example.com')['sent'])
        self.create_log.assert_not_called()

    def test_email_failure_is_audited_without_raising(self):
        self._log()
        self.send.side_effect = RuntimeError('SMTP error')
        self.assertFalse(subject.send_enrollment_terms_notice_job('LOG','school@example.com')['sent'])
        self.failed.assert_called_once()
        self.sent.assert_not_called()

    def test_staging_email_skip_does_not_mark_sent(self):
        self._log()
        self.send.return_value = {'skipped':True, 'reason':'Test site disabled'}
        self.assertTrue(subject.send_enrollment_terms_notice_job('LOG','school@example.com')['skipped'])
        self.sent.assert_not_called()


class TestTermsSettings(TestCase):
    def test_settings_preserve_plain_text_and_require_school_reply_address(self):
        for address in ('', 'school@example.com'):
            doc = Mock()
            data = {'enrollment_terms': 'Terms', 'school_email': address}
            doc.get.side_effect = data.get
            doc.set.side_effect = data.__setitem__
            with patch.object(invoice_settings,'settings_doctype_available',return_value=True), \
                 patch.object(frappe,'get_single',return_value=doc), \
                 patch.object(frappe,'throw',side_effect=ValueError), \
                 patch.object(invoice_settings,'validate_email_address') as validate, \
                 patch.object(invoice_settings,'get_invoice_settings',return_value=data):
                if address:
                    invoice_settings.update_invoice_settings({'enrollment_terms':'New terms\nTwo lines'})
                    self.assertEqual(data['enrollment_terms'],'New terms\nTwo lines')
                    validate.assert_called_once_with(address,throw=True)
                    doc.save.assert_called_once()
                else:
                    with self.assertRaises(ValueError):
                        invoice_settings.update_invoice_settings({'enrollment_terms':'Terms'})
                    doc.save.assert_not_called()


class TestConversionKeepsExistingFlow(TestCase):
    def test_trial_conversion_creates_records_then_queues_terms_before_commit(self):
        from qas_custom.modules.workflows import trial_conversion as workflow
        inquiry = frappe._dict(name='INQ',student='STUDENT',parent='PARENT')
        enrollment, invoice = frappe._dict(name='ENR'), frappe._dict(name='INV')
        context = {'session':frappe._dict(name='CS'), 'timeslot':frappe._dict(name='W'),
                   'course':'ART', 'term':'T4', 'remaining_sessions':[frappe._dict(name='CS')]}
        calls=[]
        with ExitStack() as stack:
            stack.enter_context(patch.object(frappe,'db',Mock()))
            stack.enter_context(patch.object(workflow,'_get_full_term_conversion_context',return_value=context))
            for name in ('clear_frappe_messages','apply_conversion_invoice_note','link_invoice_to_enrollment',
                         'add_conversion_internal_note','add_conversion_note'):
                stack.enter_context(patch.object(workflow,name))
            create = stack.enter_context(patch.object(workflow,'create_full_term_enrollment',return_value=enrollment))
            bill = stack.enter_context(patch.object(workflow,'create_prorata_invoice',return_value=invoice))
            attendance = stack.enter_context(patch.object(workflow,'create_full_term_attendance_entries'))
            stack.enter_context(patch.object(workflow,'mark_converted',return_value=inquiry))
            stack.enter_context(patch('qas_custom.modules.trial_referrals.award_referral_conversion_reward',return_value=None))
            stack.enter_context(patch('qas_custom.services.ndis_friendly.refresh_ndis_friendly_capacity_alert'))
            stack.enter_context(patch('qas_custom.services.inquiry.build_inquiry_detail',return_value={}))
            notice = stack.enter_context(patch.object(subject,'queue_enrollment_terms_notice',
                side_effect=lambda *args: calls.append('queue') or {'queued':True}))
            frappe.db.commit.side_effect=lambda: calls.append('commit')
            result=workflow._convert_inquiry_doc_to_full_term_core(inquiry,'CS',actor='admin')
        create.assert_called_once()
        bill.assert_called_once()
        attendance.assert_called_once()
        notice.assert_called_once_with(enrollment,invoice)
        self.assertEqual(calls,['queue','commit'])
        self.assertEqual(result['conversion']['enrollment'],'ENR')
        self.assertTrue(result['terms_notice']['queued'])
