from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import trial_referral_rebilling as service
from qas_custom.modules.trial_referrals import referral_summary


def fail(message):
	raise ValueError(message)


class TestTrialReferralRebilling(TestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.doc = frappe._dict(name="INQ-1", inquiry_type="Trial Lesson", status="Booked", parent="P-1", student="ST-1", trial_invoice="INV-1")
		self.doc.save = Mock()
		self.old = frappe._dict(name="INV-1", docstatus=1, grand_total=68, outstanding_amount=68,
			parent="P-1", student="ST-1", source_doctype="Inquiry", source_document="INQ-1", source_type="Trial Inquiry")
		self.new = frappe._dict(name="INV-2", grand_total=38)
		self.new.insert = Mock()
		self.db = SimpleNamespace(sql=Mock(), commit=Mock(), rollback=Mock(), exists=Mock(return_value=False))
		self.fake = SimpleNamespace(db=self.db, get_doc=Mock(return_value=self.old), copy_doc=Mock(return_value=self.new),
			session=SimpleNamespace(user="admin@example.com"), throw=Mock(side_effect=fail),
			cache=SimpleNamespace(lock=Mock(side_effect=lambda *a, **kw: nullcontext())))
		self.stack.enter_context(patch.object(service, "frappe", self.fake))
		self.stack.enter_context(patch.object(service, "_", side_effect=lambda text: text))
		self.stack.enter_context(patch.object(service, "_get_trial_inquiry", return_value=self.doc))
		self.stack.enter_context(patch.object(service, "referral_status", side_effect=lambda doc: doc.get("referral_status") or "Not Applicable"))
		self.stack.enter_context(patch.object(service, "_validated_referrer", return_value=("P-REF", "C-REF")))
		self.stack.enter_context(patch.object(service, "trial_referral_discount_amount", return_value=30))
		self.stack.enter_context(patch.object(service, "referral_invoice_message", return_value="Referral discount $30"))
		self.stack.enter_context(patch.object(service, "get_invoice_store_credit_applied", return_value=0))
		self.set_referrer = self.stack.enter_context(patch.object(service, "_set_referrer", side_effect=lambda doc, **kw: doc.update(referral_status="Verified")))
		self.link = self.stack.enter_context(patch.object(service, "_link_invoice"))
		base = "qas_custom.services.school_admin."
		self.payment = self.stack.enter_context(patch(base+"_invoice_payment_amount", return_value=0))
		self.cancel = self.stack.enter_context(patch(base+"cancel_school_admin_invoice_data", return_value={}))
		self.submit = self.stack.enter_context(patch(base+"submit_school_admin_invoice_data", return_value={"notification": {"queued": True}}))
		self.adjust = self.stack.enter_context(patch(base+"_apply_invoice_adjustments"))
		self.stack.enter_context(patch(base+"_invoice_adjustment_rows", return_value=[]))
		self.stack.enter_context(patch(base+"disable_sales_invoice_auto_notifications"))
		self.stack.enter_context(patch(base+"_run_school_admin_invoice_mutation", side_effect=lambda fn: fn()))
		self.stack.enter_context(patch(base+"_add_comment"))
		self.notify = self.stack.enter_context(patch("qas_custom.modules.notifications.trial_referral_notifications.queue_referral_trial_discount_notification"))

	def test_replaces_submits_and_links_same_inquiry_before_single_commit(self):
		result = service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.assertEqual(result["invoice"], "INV-2")
		self.assertEqual(result["old_invoice"], "INV-1")
		self.assertEqual(self.new.source_document, "INQ-1")
		self.assertEqual(self.new.amended_from, "INV-1")
		self.assertEqual(self.new.parent, "P-1")
		self.cancel.assert_called_once()
		self.assertFalse(self.cancel.call_args.kwargs["commit"])
		self.assertFalse(self.cancel.call_args.kwargs["send_notifications"])
		self.submit.assert_called_once_with(invoice="INV-2", enqueue_notification=True, commit=False)
		self.link.assert_called_once_with("INQ-1", "INV-2")
		self.db.commit.assert_called_once()
		self.db.rollback.assert_not_called()
		self.notify.assert_called_once_with("INQ-1")

	def test_submission_failure_rolls_back_cancellation_and_referral(self):
		self.submit.side_effect = RuntimeError("submission failed")
		with self.assertRaisesRegex(RuntimeError, "submission failed"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_called_once()
		self.db.commit.assert_not_called()
		self.db.rollback.assert_called_once()
		self.notify.assert_not_called()

	def test_double_request_does_not_cancel_replacement(self):
		service.add_invoiced_trial_referral("INQ-1", "P-REF")
		with self.assertRaisesRegex(ValueError, "already has a verified referral"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_called_once()
		self.submit.assert_called_once()

	def test_existing_adjustments_are_preserved(self):
		with patch("qas_custom.services.school_admin._invoice_adjustment_rows", return_value=[frappe._dict(description="Earlier discount", tax_amount=-5)]):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.assertEqual(self.adjust.call_args.args[1], [{"description": "Earlier discount", "amount": -5}, {"description": "Referral discount", "amount": -30}])

	def test_wrong_total_rolls_back_before_submission(self):
		self.new.grand_total = 45
		with self.assertRaisesRegex(ValueError, "replacement total"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.submit.assert_not_called()
		self.db.rollback.assert_called_once()

	def test_paid_invoice_is_rejected_before_cancellation(self):
		self.payment.return_value = 68
		with self.assertRaisesRegex(ValueError, "payment or Store Credit"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()
		self.set_referrer.assert_not_called()

	def test_partial_payment_is_rejected(self):
		self.old.outstanding_amount = 50
		with self.assertRaisesRegex(ValueError, "payment or Store Credit"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_store_credit_is_rejected(self):
		with patch.object(service, "get_invoice_store_credit_applied", return_value=10):
			with self.assertRaisesRegex(ValueError, "payment or Store Credit"):
				service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_other_inquiry_invoice_is_rejected(self):
		self.old.source_document = "INQ-OTHER"
		with self.assertRaisesRegex(ValueError, "does not belong"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_other_family_invoice_is_rejected(self):
		self.old.parent = "P-OTHER"
		with self.assertRaisesRegex(ValueError, "does not belong"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_unresolved_stripe_attempt_is_rejected(self):
		self.db.exists.return_value = True
		with self.assertRaisesRegex(ValueError, "Stripe checkout"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_converted_inquiry_uses_existing_historical_path(self):
		self.doc.status = "Converted"
		with self.assertRaisesRegex(ValueError, "Only a booked or completed trial"):
			service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.cancel.assert_not_called()

	def test_late_referral_action_available_without_original_referral_claim(self):
		with patch("qas_custom.modules.trial_referrals._existing_referral_reward", return_value=None), patch("qas_custom.modules.trial_referrals.trial_referral_discount_amount", return_value=30), patch("qas_custom.modules.trial_referrals.referral_conversion_reward_amount", return_value=30):
			summary = referral_summary(self.doc)
		self.assertTrue(summary["can_add_to_invoiced_trial"])
		self.assertFalse(summary["can_verify"])


	def test_completed_trial_can_replace_invoice_without_reopening_inquiry(self):
		self.doc.status = "Completed"
		result = service.add_invoiced_trial_referral("INQ-1", "P-REF")
		self.assertEqual(result["invoice"], "INV-2")
		self.assertEqual(self.doc.status, "Completed")
		self.link.assert_called_once_with("INQ-1", "INV-2")
		self.submit.assert_called_once_with(invoice="INV-2", enqueue_notification=True, commit=False)

	def test_late_referral_action_is_visible_for_completed_trial(self):
		self.doc.status = "Completed"
		with patch("qas_custom.modules.trial_referrals._existing_referral_reward", return_value=None), patch("qas_custom.modules.trial_referrals.trial_referral_discount_amount", return_value=30), patch("qas_custom.modules.trial_referrals.referral_conversion_reward_amount", return_value=30):
			self.assertTrue(referral_summary(self.doc)["can_add_to_invoiced_trial"])


class TestReferralRebillingDeferredCommit(TestCase):
	def test_submission_can_leave_commit_to_rebilling_transaction(self):
		from qas_custom.services import school_admin as admin
		invoice = frappe._dict(name="INV-2", docstatus=0)
		invoice.flags = SimpleNamespace(ignore_permissions=False)
		invoice.submit = Mock()
		db = SimpleNamespace(savepoint=Mock(), commit=Mock(), rollback=Mock())
		fake = SimpleNamespace(db=db, get_doc=Mock(return_value=invoice))
		with ExitStack() as stack:
			stack.enter_context(patch.object(admin, "frappe", fake))
			stack.enter_context(patch.object(admin, "_require_school_admin"))
			stack.enter_context(patch.object(admin, "lock_payg_operations_for_invoices", return_value=[]))
			stack.enter_context(patch.object(admin, "reject_payg_support_view_write"))
			stack.enter_context(patch.object(admin, "validate_payg_bindings"))
			stack.enter_context(patch.object(admin, "_lock_school_admin_draft_invoice", return_value=invoice))
			stack.enter_context(patch.object(admin, "apply_invoice_payment_snapshot", return_value=False))
			stack.enter_context(patch.object(admin, "_run_school_admin_invoice_mutation", side_effect=lambda fn: fn()))
			stack.enter_context(patch.object(admin, "_add_comment"))
			stack.enter_context(patch.object(admin, "apply_store_credit_to_invoice", return_value={"applied": 0}))
			stack.enter_context(patch.object(admin, "sync_invoice_store_credit_snapshot"))
			stack.enter_context(patch.object(admin, "get_invoice_store_credit_applied", return_value=0))
			notify = stack.enter_context(patch.object(admin, "_enqueue_invoice_notification", return_value={"queued": True}))
			stack.enter_context(patch.object(admin, "_enqueue_paid_receipt", return_value={}))
			stack.enter_context(patch.object(admin, "_build_invoice_payload", return_value={}))
			admin.submit_school_admin_invoice_data("INV-2", enqueue_notification=True, commit=False)
		invoice.submit.assert_called_once()
		notify.assert_called_once()
		db.commit.assert_not_called()

	def test_cancellation_can_leave_commit_to_rebilling_transaction(self):
		from qas_custom.services import school_admin as admin
		invoice = frappe._dict(name="INV-1", docstatus=1)
		db = SimpleNamespace(commit=Mock())
		fake = SimpleNamespace(db=db, get_doc=Mock(return_value=invoice))
		with ExitStack() as stack:
			stack.enter_context(patch.object(admin, "frappe", fake))
			stack.enter_context(patch.object(admin, "_", side_effect=lambda text: text))
			stack.enter_context(patch.object(admin, "_require_invoice_cancellation_actor"))
			stack.enter_context(patch("qas_custom.services.order_invoices.is_order_invoice", return_value=False))
			stack.enter_context(patch.object(admin, "payment_mutations_enabled", return_value=True))
			stack.enter_context(patch.object(admin, "_invoice_payment_amount", return_value=0))
			stack.enter_context(patch.object(admin, "get_invoice_store_credit_applied", return_value=0))
			stack.enter_context(patch.object(admin, "_cancel_invoice_payment_entries"))
			stack.enter_context(patch.object(admin, "cancel_store_credit_journal_entries"))
			stack.enter_context(patch.object(admin, "_reverse_invoice_store_credit_application", return_value=None))
			stack.enter_context(patch.object(admin, "_create_invoice_cancellation_store_credit", return_value=None))
			cancel = stack.enter_context(patch.object(admin, "_cancel_submitted_invoice_as_admin"))
			stack.enter_context(patch.object(admin, "_clear_deleted_invoice_enrollment_snapshot"))
			stack.enter_context(patch.object(admin, "_add_comment"))
			stack.enter_context(patch.object(admin, "_skipped_invoice_notification", return_value={}))
			stack.enter_context(patch.object(admin, "_build_invoice_payload", return_value={}))
			admin.cancel_school_admin_invoice_data("INV-1", reason="Late referral", send_notifications=False, commit=False)
		cancel.assert_called_once_with("INV-1")
		db.commit.assert_not_called()
