from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services import marketing_notifications as marketing
from qas_custom.services import inquiry as inquiries


class MarketingTests(TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.exists.return_value = False
        self.db.sql.return_value = [("notification-1",)]
        for target, value in (("frappe.db", self.db), ("frappe.conf", {}),
                              ("frappe.log_error", Mock()), ("frappe.get_traceback", Mock(return_value="test error"))):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = SimpleNamespace(enabled=1, recipient="agency@example.com")
        single_patcher = patch("frappe.get_single", return_value=self.settings)
        self.get_single = single_patcher.start()
        self.addCleanup(single_patcher.stop)
        self.doc = SimpleNamespace(name="notification-1", status="Pending", email_queue=None,
                                   parent_name="Jane <Smith>", recipient="agency@example.com",
                                   save=Mock(), last_error="")

    def test_settings_default_off_and_blank_is_valid(self):
        self.assertEqual(marketing.validate_settings(0, ""), (0, ""))
        self.assertEqual(marketing.validate_settings(1, " AGENCY@EXAMPLE.COM "), (1, "agency@example.com"))

    @patch("frappe.throw", side_effect=ValueError)
    def test_settings_reject_missing_invalid_or_multiple_addresses(self, throw):
        for enabled, recipient in [(1, ""), (1, "bad"), (1, "a@example.com,b@example.com"),
                                   (0, "a@example.com;b@example.com"), (1, "Name <a@example.com>"),
                                   (1, "a@example.com\nbcc@example.com"), (2, "a@example.com")]:
            with self.subTest(enabled=enabled, recipient=recipient), self.assertRaises(ValueError):
                marketing.validate_settings(enabled, recipient)

    def test_exact_outbound_body_is_only_fixed_text_and_escaped_parent_name(self):
        self.assertEqual(marketing.email_body("Jane <img src=x> & Smith"),
                         "<p>A new website trial enquiry has been received.</p><p>Parent name: Jane &lt;img src=x&gt; &amp; Smith</p>")
        self.assertEqual(marketing.SUBJECT, "New website trial enquiry")
        self.assertNotIn("href", marketing.email_body("Jane"))

    def test_settings_apis_check_roles_before_access(self):
        with patch("qas_custom.services.school_admin._require_school_admin", side_effect=PermissionError):
            with self.assertRaises(PermissionError): marketing.get_settings()
            with self.assertRaises(PermissionError): marketing.save_settings(1, "agency@example.com")
        self.get_single.assert_not_called()

    def test_support_view_cannot_change_settings(self):
        with patch("qas_custom.services.school_admin._require_school_admin"), patch(
                "qas_custom.services.support_view.reject_support_view_write", side_effect=PermissionError):
            with self.assertRaises(PermissionError): marketing.save_settings(1, "agency@example.com")
        self.get_single.assert_not_called()

    def test_save_normalizes_and_persists_without_sending(self):
        self.settings.save = Mock()
        with patch("qas_custom.services.school_admin._require_school_admin"), patch(
                "qas_custom.services.support_view.reject_support_view_write"), patch.object(marketing, "sendmail_or_skip") as send:
            result = marketing.save_settings(1, " New@Example.com ")
        self.assertEqual(result, {"enabled": 1, "recipient": "new@example.com"})
        self.settings.save.assert_called_once_with(ignore_permissions=True)
        send.assert_not_called()
        self.db.commit.assert_not_called()

    def test_disabled_settings_create_no_history_or_job(self):
        self.settings.enabled = 0
        with patch("frappe.get_doc") as get_doc, patch("frappe.enqueue") as enqueue:
            marketing.record_webhook_trial("INQ-1")
        get_doc.assert_not_called()
        enqueue.assert_not_called()

    def test_non_trial_and_missing_submission_are_excluded(self):
        for inquiry_type, key in [("School Visit", "web:1"), ("Direct Enrollment", "web:1"), ("Trial Lesson", "")]:
            with self.subTest(inquiry_type=inquiry_type, key=key), patch("frappe.get_doc", return_value=frappe._dict(
                    inquiry_type=inquiry_type, external_submission_id=key)), patch("frappe.enqueue") as enqueue:
                marketing.record_webhook_trial("INQ-1")
                enqueue.assert_not_called()

    def test_capture_has_only_name_and_agency_and_enqueues_after_commit(self):
        inquiry = frappe._dict(inquiry_type="Trial Lesson", external_submission_id="web:1", contact_name="Jane",
                              contact_email="private@example.com", contact_phone="0400000000", student="Secret child", status="Needs Review")
        delivery = Mock(name="delivery")
        delivery.name = "notification-1"
        delivery.insert.return_value = delivery
        with patch("frappe.get_doc", side_effect=[inquiry, delivery]) as get_doc, patch("frappe.enqueue") as enqueue:
            marketing.record_webhook_trial("INQ-1")
        payload = get_doc.call_args_list[1].args[0]
        self.assertEqual(set(payload), {"doctype", "name", "inquiry", "recipient", "parent_name", "status"})
        self.assertEqual(payload["parent_name"], "Jane")
        self.assertEqual(payload["recipient"], "agency@example.com")
        self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
        self.db.commit.assert_not_called()

    def test_existing_record_does_not_enqueue_again(self):
        self.db.exists.return_value = True
        with patch("frappe.get_doc", return_value=frappe._dict(inquiry_type="Trial Lesson", external_submission_id="web:1")), patch("frappe.enqueue") as enqueue:
            marketing.record_webhook_trial("INQ-1")
        enqueue.assert_not_called()

    def test_job_queue_failure_leaves_pending_record_for_recovery(self):
        inquiry = frappe._dict(inquiry_type="Trial Lesson", external_submission_id="web:1", contact_name="Jane")
        delivery = Mock(); delivery.name = "notification-1"; delivery.insert.return_value = delivery
        with patch("frappe.get_doc", side_effect=[inquiry, delivery]), patch("frappe.enqueue", side_effect=RuntimeError("Redis down")):
            marketing.record_webhook_trial("INQ-1")
        self.db.rollback.assert_not_called()

    def test_record_failure_rolls_back_only_notification_savepoint(self):
        with patch("frappe.get_doc", side_effect=RuntimeError("insert failed")), patch("frappe.enqueue") as enqueue:
            marketing.record_webhook_trial("INQ-1")
        self.db.rollback.assert_called_once_with(save_point="marketing_trial_record")
        enqueue.assert_not_called()

    def worker(self, send=None):
        with patch("frappe.get_doc", side_effect=lambda doctype, *args, **kwargs: self.settings if doctype == marketing.SETTINGS else self.doc), patch.object(marketing, "outbound_email_enabled", return_value=True), patch.object(
                marketing, "sendmail_or_skip", **({"side_effect": send} if isinstance(send, Exception) else {"return_value": send or SimpleNamespace(name="EMAIL-1")})) as mail:
            marketing.queue_delivery("notification-1")
        return mail

    def test_worker_locks_record_and_only_queues_minimal_email(self):
        mail = self.worker()
        self.assertIn("FOR UPDATE", self.db.sql.call_args.args[0])
        self.assertEqual(self.db.sql.call_args.args[1], ("notification-1",))
        self.assertEqual(mail.call_args.kwargs, {
            "action": "marketing_trial_new", "recipients": ["agency@example.com"],
            "subject": "New website trial enquiry", "message": marketing.email_body("Jane <Smith>"),
            "delayed": True, "now": False, "add_unsubscribe_link": 0, "is_notification": True,
        })
        self.assertEqual((self.doc.status, self.doc.email_queue), ("Queued", "EMAIL-1"))
        self.db.commit.assert_not_called()

    def test_worker_uses_fresh_locking_reads_for_delivery_and_settings(self):
        with patch("frappe.get_doc", side_effect=lambda doctype, *args, **kwargs: self.settings if doctype == marketing.SETTINGS else self.doc) as get_doc, patch.object(marketing, "outbound_email_enabled", return_value=True), patch.object(marketing, "sendmail_or_skip", return_value=SimpleNamespace(name="EMAIL-1")):
            marketing.queue_delivery("notification-1")
        self.assertEqual(get_doc.call_args_list[0].args, (marketing.DELIVERY, "notification-1"))
        self.assertEqual(get_doc.call_args_list[1].args, (marketing.SETTINGS, marketing.SETTINGS))
        for call in get_doc.call_args_list:
            self.assertTrue(call.kwargs["for_update"])

    def test_repeated_worker_does_not_create_another_email(self):
        self.worker().assert_called_once()
        self.worker().assert_not_called()

    def test_missing_or_rolled_back_record_is_not_sent(self):
        self.db.sql.return_value = []
        self.worker().assert_not_called()

    def test_disabled_or_changed_recipient_cancels_pending_handoff(self):
        for enabled, recipient in [(0, "agency@example.com"), (1, "new@example.com")]:
            self.doc.status = "Pending"
            self.settings.enabled, self.settings.recipient = enabled, recipient
            self.worker().assert_not_called()
            self.assertEqual(self.doc.status, "Skipped")

    def test_staging_cannot_queue_real_mail(self):
        with patch("frappe.get_doc", side_effect=lambda doctype, *args, **kwargs: self.settings if doctype == marketing.SETTINGS else self.doc), patch.object(marketing, "outbound_email_enabled", return_value=False), patch.object(marketing, "sendmail_or_skip") as mail:
            marketing.queue_delivery("notification-1")
        mail.assert_not_called()
        self.assertEqual(self.doc.status, "Skipped")

    def test_failed_handoff_rolls_back_email_and_retries_same_record(self):
        self.worker(RuntimeError("SMTP account missing"))
        self.assertEqual(self.doc.status, "Failed")
        self.db.rollback.assert_called_once_with(save_point="marketing_trial_email")
        self.worker().assert_called_once()
        self.assertEqual(self.doc.status, "Queued")

    def test_recovery_processes_only_pending_and_failed_and_commits_each(self):
        with patch.object(marketing, "scheduler_enabled", return_value=True), patch("frappe.get_all", return_value=["N1", "N2"]) as get_all, patch.object(marketing, "queue_delivery") as worker:
            marketing.retry_pending()
        self.assertEqual(get_all.call_args.kwargs["filters"], {"status": ["in", ["Pending", "Failed"]]})
        self.assertEqual([c.args[0] for c in worker.call_args_list], ["N1", "N2"])
        self.assertEqual(self.db.commit.call_count, 2)

    def test_scheduler_disabled_means_no_recovery(self):
        with patch.object(marketing, "scheduler_enabled", return_value=False), patch("frappe.get_all") as get_all:
            marketing.retry_pending()
        get_all.assert_not_called()


class WebhookScopeTests(TestCase):
    def call_webhook(self, duplicate=None, core_error=None):
        with patch.object(inquiries, "_get_payload", return_value={}), patch.object(inquiries, "_validate_webhook_token"), patch.object(
                inquiries, "_normalize_webhook_payload", return_value={"external_submission_id": "web:1"}), patch.object(
                inquiries, "_get_existing_webhook_inquiry", return_value=duplicate), patch.object(
                inquiries, "create_inquiry_core", return_value={"inquiry": {"id": "INQ-1"}}, side_effect=core_error) as core, patch.object(
                inquiries, "_build_webhook_response", return_value={}), patch.object(marketing, "record_webhook_trial") as record:
            if core_error:
                with self.assertRaises(RuntimeError): inquiries.create_inquiry_webhook_data({})
            else:
                inquiries.create_inquiry_webhook_data({})
        return core, record

    def test_new_webhook_records_before_transaction_commit(self):
        core, record = self.call_webhook()
        self.assertFalse(core.call_args.kwargs["commit"])
        record.assert_called_once_with("INQ-1")

    def test_replayed_webhook_does_not_record_or_create(self):
        core, record = self.call_webhook(duplicate="INQ-1")
        core.assert_not_called(); record.assert_not_called()

    def test_failed_application_never_records_notification(self):
        _, record = self.call_webhook(core_error=RuntimeError("invalid submission"))
        record.assert_not_called()

    def test_school_visit_does_not_record_marketing(self):
        with patch.object(inquiries, "_get_payload", return_value={}), patch.object(inquiries, "_validate_webhook_token"), patch.object(
                inquiries, "_normalize_school_visit_webhook_payload", return_value={"external_submission_id": "visit:1"}), patch.object(
                inquiries, "_get_existing_webhook_inquiry", return_value=None), patch.object(
                inquiries, "create_inquiry_core", return_value={"inquiry": {"id": "INQ-VISIT"}}), patch.object(
                inquiries, "_build_webhook_response"), patch.object(marketing, "record_webhook_trial") as record:
            inquiries.create_school_visit_webhook_data({})
        record.assert_not_called()

    def test_manual_create_does_not_record_marketing(self):
        with patch.object(inquiries, "_require_admin"), patch.object(inquiries, "_get_payload", return_value={}), patch.object(
                inquiries, "create_inquiry_core"), patch.object(inquiries.frappe, "session", SimpleNamespace(user="admin")), patch.object(marketing, "record_webhook_trial") as record:
            inquiries.create_inquiry_data({})
        record.assert_not_called()
