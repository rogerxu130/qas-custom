"""Welcome email content, transaction boundaries and duplicate protection."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.modules.notifications import payg_parent_notifications as mail


class TestPaygWelcome(TestCase):
    def setUp(self):
        self.card = frappe._dict(name="CARD-1", family_parent="P-1", course="Anime & Art <Beginner>",
                                 issued_on="2026-10-17", expires_on="2027-04-17")
        self.recipient = {"email": "parent@example.test", "parent_name": "Ava <Family>"}
        self.fake = SimpleNamespace(db=SimpleNamespace(savepoint=Mock(), rollback=Mock(),
                                    exists=Mock(return_value=False)), log_error=Mock(),
                                    get_traceback=lambda: "mail failure")
        patches = [patch.object(mail, "frappe", self.fake),
                   patch.object(mail, "_parent_recipient", return_value=self.recipient),
                   patch.object(mail, "_parent_portal_url", side_effect=lambda path: "https://portal.example.test" + path),
                   patch.object(mail, "sendmail_or_skip", return_value=SimpleNamespace(name="EMAIL-1")),
                   patch.object(mail, "_create_notification_log", return_value="LOG-1"),
                   patch.object(mail, "_mark_notification_queued")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_queues_to_card_family_with_correct_login_and_escaped_details(self):
        mail.queue_welcome_notification(self.card)
        mail._parent_recipient.assert_called_once_with("P-1")
        args = mail.sendmail_or_skip.call_args.kwargs
        self.assertEqual(args["recipients"], ["parent@example.test"])
        self.assertEqual((args["reference_doctype"], args["reference_name"]), ("QAS PAYG Card", "CARD-1"))
        self.assertFalse(args["now"])
        self.assertTrue(args["delayed"])
        for text in ("https://portal.example.test/psugo-login", "Anime &amp; Art &lt;Beginner&gt;",
                     "Ava &lt;Family&gt;", "17 April 2027", "17 October 2026", "10 class visits",
                     "at least 72 hours", "more than 72 hours", "At exactly 72 hours", "Forgot password"):
            self.assertIn(text, args["message"])
        mail._mark_notification_queued.assert_called_once_with("LOG-1")
        self.fake.db.rollback.assert_not_called()

    def test_existing_notification_is_not_sent_again(self):
        self.fake.db.exists.return_value = True
        mail.queue_welcome_notification(self.card)
        mail.sendmail_or_skip.assert_not_called()
        mail._create_notification_log.assert_not_called()

    def test_missing_family_email_logs_failure_without_failing_issue(self):
        self.recipient["email"] = ""
        mail.queue_welcome_notification(self.card)
        mail.sendmail_or_skip.assert_not_called()
        self.fake.db.rollback.assert_called_once_with(save_point="payg_welcome_email")
        self.fake.log_error.assert_called_once()

    def test_queue_failure_is_isolated_from_card_transaction(self):
        mail.sendmail_or_skip.side_effect = RuntimeError("mail unavailable")
        mail.queue_welcome_notification(self.card)
        self.fake.db.rollback.assert_called_once_with(save_point="payg_welcome_email")
        mail._create_notification_log.assert_not_called()

    def test_missing_queue_or_log_rolls_back_email_instead_of_marking_sent(self):
        for failure in ("queue", "log"):
            with self.subTest(failure=failure):
                self.fake.db.rollback.reset_mock()
                mail.sendmail_or_skip.return_value = None if failure == "queue" else SimpleNamespace(name="EMAIL")
                mail._create_notification_log.return_value = None
                mail.queue_welcome_notification(self.card)
                self.fake.db.rollback.assert_called_once_with(save_point="payg_welcome_email")
        mail._mark_notification_queued.assert_not_called()

    def test_environment_skip_does_not_claim_delivery(self):
        mail.sendmail_or_skip.return_value = {"skipped": True, "reason": "Staging"}
        mail.queue_welcome_notification(self.card)
        mail._create_notification_log.assert_not_called()
        mail._mark_notification_queued.assert_not_called()
        self.fake.db.rollback.assert_not_called()
