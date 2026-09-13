from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace
import frappe
from qas_custom.modules.notifications import store_order_notifications as service
from qas_custom.qas_custom.doctype.store_order.store_order import StoreOrder

class TestStoreOrderAdminNotification(TestCase):
    def setUp(self):
        self.campus_patch = patch.object(service, "campus_admin_order_recipients", return_value=[])
        self.campus_patch.start()
        self.addCleanup(self.campus_patch.stop)
        self.enqueue_patch = patch.object(frappe, 'enqueue')
        self.enqueue = self.enqueue_patch.start()
        self.addCleanup(self.enqueue_patch.stop)

    def test_only_enabled_school_admin_users_and_unique_emails(self):
        with patch.object(frappe, 'get_all', side_effect=[['admin1','admin2'], ['Admin@example.com','admin@example.com',None]]) as query:
            self.assertEqual(service.school_admin_order_recipients(), ['admin@example.com'])
        self.assertEqual(query.call_args_list[0].kwargs['filters'], {'role':'School Admin','parenttype':'User'})
        self.assertEqual(query.call_args_list[1].kwargs['filters'], {'name':['in',['admin1','admin2']], 'enabled':1})

    def test_no_roles_means_no_recipients(self):
        with patch.object(frappe, 'get_all', return_value=[]) as query:
            self.assertEqual(service.school_admin_order_recipients(), [])
        query.assert_called_once()

    def test_queue_failure_does_not_fail_order_and_environment_skip_is_respected(self):
        for result in (SimpleNamespace(name='Q1'), {'skipped':True}, None):
            db = SimpleNamespace(savepoint=Mock(), rollback=Mock())
            with patch.object(frappe, 'db', db), patch.object(service, 'school_admin_order_recipients', return_value=['admin@example.com']), patch.object(service, 'new_order_email_content', return_value='body'), patch.object(service, 'sendmail_or_skip', return_value=result) as send, patch.object(frappe, 'log_error') as log, patch.object(frappe, 'get_traceback', return_value='failure'):
                service.queue_new_order_admin_notification(SimpleNamespace(name='O1', pickup_campus='North'))
                self.assertEqual(send.call_args.kwargs['recipients'], ['admin@example.com'])
                self.assertFalse(send.call_args.kwargs['now'])
                self.assertTrue(send.call_args.kwargs['delayed'])
                self.assertEqual(log.call_count, int(result is None))
                self.assertEqual(db.rollback.call_count, int(result is None))

    def test_missing_recipient_is_logged(self):
        with patch.object(frappe, 'db', SimpleNamespace(savepoint=Mock(), rollback=Mock())), patch.object(service, 'school_admin_order_recipients', return_value=[]), patch.object(service, 'sendmail_or_skip') as send, patch.object(frappe, 'log_error') as log, patch.object(frappe, 'get_traceback', return_value='missing recipient'):
            service.queue_new_order_admin_notification(SimpleNamespace(name='O1', pickup_campus='North'))
        send.assert_not_called()
        log.assert_called_once()

    def test_insert_hook_queues_notification(self):
        order = SimpleNamespace(name='O1', pickup_campus='North')
        with patch.object(service, 'queue_new_order_admin_notification') as queue:
            StoreOrder.after_insert(order)
        queue.assert_called_once_with(order)

    def test_email_has_order_details_and_escapes_user_content(self):
        doc=frappe._dict(name='O1', parent='P1', pickup_campus='Campus <One>', items=[frappe._dict(product_name='<script>Kit</script>',qty=2,amount=158)])
        with patch.object(frappe, 'db', SimpleNamespace(get_value=Mock(return_value='Parent & Child'))), patch.object(service, '_parent_portal_url', side_effect=lambda path:'https://system.example'+path):
            html=service.new_order_email_content(doc)
        for expected in ['O1','Parent &amp; Child','Campus &lt;One&gt;','A$158.00','/school-admin?tab=materials&amp;order=O1','&lt;script&gt;']:
            self.assertIn(expected,html)
        self.assertNotIn('<script>',html)

    def test_immediate_delivery_respects_environment(self):
        for enabled in (True, False):
            with patch.object(service, 'outbound_email_enabled', return_value=enabled), patch.object(frappe, 'get_doc') as get:
                service.send_new_order_email_queue('Q1')
                if enabled:
                    get.assert_called_once_with('Email Queue', 'Q1')
                    get.return_value.send.assert_called_once_with()
                else:
                    get.assert_not_called()

    def test_new_order_dispatch_waits_for_commit(self):
        with patch.object(frappe, 'db', SimpleNamespace(savepoint=Mock(), rollback=Mock())), patch.object(service, 'school_admin_order_recipients', return_value=['admin@example.com']), patch.object(service, 'new_order_email_content', return_value='body'), patch.object(service, 'sendmail_or_skip', return_value=SimpleNamespace(name='Q1')):
            service.queue_new_order_admin_notification(SimpleNamespace(name='O1', pickup_campus='North'))
        self.enqueue.assert_called_once_with('qas_custom.modules.notifications.store_order_notifications.send_new_order_email_queue', queue='short', enqueue_after_commit=True, queue_name='Q1')

    def test_both_admin_groups_receive_their_own_portal_link(self):
        doc = frappe._dict(name='O1', parent='P1', pickup_campus='South', items=[])
        db = SimpleNamespace(savepoint=Mock(), rollback=Mock(), get_value=Mock(return_value='Family'))
        with patch.object(frappe, 'db', db), patch.object(service, 'school_admin_order_recipients', return_value=['school@example.com']), patch.object(service, 'campus_admin_order_recipients', return_value=['campus@example.com']) as campus, patch.object(service, '_parent_portal_url', side_effect=lambda path:'https://portal.example'+path), patch.object(service, 'sendmail_or_skip', return_value=SimpleNamespace(name='Q1')) as send:
            service.queue_new_order_admin_notification(doc)
        campus.assert_called_once_with('South')
        self.assertEqual(send.call_count, 2)
        school_mail, campus_mail = [call.kwargs for call in send.call_args_list]
        self.assertEqual(school_mail['recipients'], ['school@example.com'])
        self.assertIn('/school-admin?tab=materials', school_mail['message'])
        self.assertEqual(campus_mail['recipients'], ['campus@example.com'])
        self.assertIn('/campus-admin?tab=orders', campus_mail['message'])
        self.assertEqual(self.enqueue.call_count, 2)
