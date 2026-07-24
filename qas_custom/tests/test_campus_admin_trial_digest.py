from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.modules.notifications.campus_admin_trial_digest import (
	_assign_trials_to_recipients,
	_build_eligible_trial_rows,
	_digest_message,
	_digest_subject,
	campus_admin_trial_digest_event_key,
	classify_trial_invoice_payment,
	enqueue_campus_admin_next_day_trial_digests,
	run_campus_admin_next_day_trial_digests,
	send_campus_admin_next_day_trial_digest_job,
)


def invoice(*, docstatus=1, status="Unpaid", outstanding=68, payable=68):
	return {
		"name": "ACC-SINV-TEST",
		"docstatus": docstatus,
		"status": status,
		"outstanding_amount": outstanding,
		"payable_total": payable,
	}


def trial_row(campus="Indooroopilly", **overrides):
	row = {
		"inquiry": "INQ-001",
		"student_name": "Ava Test",
		"campus": campus,
		"course": "Anime Art",
		"start_time": "16:00",
		"end_time": "17:30",
		"payment_status": "Paid",
		"outstanding_amount": 0,
	}
	row.update(overrides)
	return row


class TestCampusAdminTrialDigest(TestCase):
	def test_payment_classification(self):
		self.assertEqual(classify_trial_invoice_payment(None)["status"], "Payment Required at Reception")
		self.assertEqual(
			classify_trial_invoice_payment(invoice(docstatus=0))["status"],
			"Payment Required at Reception",
		)
		self.assertEqual(
			classify_trial_invoice_payment(invoice(docstatus=2, status="Cancelled"))["status"],
			"Payment Required at Reception",
		)
		self.assertEqual(classify_trial_invoice_payment(invoice(outstanding=0))["status"], "Paid")
		self.assertEqual(
			classify_trial_invoice_payment(invoice(outstanding=20, payable=68)),
			{"status": "Partially Paid", "outstanding_amount": 20},
		)
		self.assertEqual(
			classify_trial_invoice_payment(invoice(outstanding=68, payable=68)),
			{"status": "Unpaid", "outstanding_amount": 68},
		)

	def test_builds_only_current_attending_trial_inquiries(self):
		sessions = [
			{"name": "CS-001", "weekly_timeslot": "WT-001"},
			{"name": "CS-002", "weekly_timeslot": "WT-002"},
		]
		timeslots = {
			"WT-001": {
				"course": "Anime Art",
				"campus": "Indooroopilly",
				"start_time": "16:00:00",
				"end_time": "17:30:00",
			},
			"WT-002": {
				"course": "Creative Art",
				"campus": "Upper Mount Gravatt",
				"start_time": "10:00:00",
				"end_time": "11:30:00",
			},
		}
		attendance = [
			{
				"course_session": "CS-001",
				"student": "STU-001",
				"enrollment_type": "Trial",
				"status": "To be started",
				"source_doctype": "Inquiry",
				"source_document": "INQ-001",
			},
			{
				"course_session": "CS-002",
				"student": "STU-002",
				"enrollment_type": "Trial",
				"status": "To be started",
				"source_doctype": "Inquiry",
				"source_document": "INQ-002",
			},
			{
				"course_session": "CS-001",
				"student": "STU-003",
				"enrollment_type": "Trial",
				"status": "Leave",
				"source_doctype": "Inquiry",
				"source_document": "INQ-003",
			},
			{
				"course_session": "CS-001",
				"student": "STU-004",
				"enrollment_type": "Full-Term",
				"status": "To be started",
				"source_doctype": "Enrollment",
				"source_document": "ENR-001",
			},
			{
				"course_session": "CS-001",
				"student": "STU-005",
				"enrollment_type": "Trial",
				"status": "To be started",
				"source_doctype": "Inquiry",
				"source_document": "INQ-005",
			},
		]
		inquiries = {
			"INQ-001": {
				"name": "INQ-001",
				"inquiry_type": "Trial Lesson",
				"status": "Booked",
				"course_session": "CS-001",
				"student": "STU-001",
				"trial_invoice": "INV-001",
			},
			"INQ-002": {
				"name": "INQ-002",
				"inquiry_type": "Trial Lesson",
				"status": "Rescheduled",
				"course_session": "CS-002",
				"student": "STU-002",
				"trial_invoice": None,
			},
			"INQ-003": {
				"name": "INQ-003",
				"inquiry_type": "Trial Lesson",
				"status": "Booked",
				"course_session": "CS-001",
				"student": "STU-003",
				"trial_invoice": None,
			},
			"INQ-005": {
				"name": "INQ-005",
				"inquiry_type": "Trial Lesson",
				"status": "Booked",
				"course_session": "CS-002",
				"student": "STU-005",
				"trial_invoice": None,
			},
		}
		rows = _build_eligible_trial_rows(
			sessions,
			timeslots,
			attendance,
			inquiries,
			{"STU-001": "Ava Test", "STU-002": "Ben Test"},
			{"INV-001": invoice(outstanding=0)},
		)

		self.assertEqual([row["inquiry"] for row in rows], ["INQ-001", "INQ-002"])
		self.assertEqual(rows[0]["payment_status"], "Paid")
		self.assertEqual(rows[1]["payment_status"], "Payment Required at Reception")
		self.assertEqual(rows[1]["student_name"], "Ben Test")

	def test_deduplicates_same_inquiry_and_session(self):
		sessions = [{"name": "CS-001", "weekly_timeslot": "WT-001"}]
		timeslots = {
			"WT-001": {
				"course": "Anime Art",
				"campus": "Indooroopilly",
				"start_time": "16:00:00",
				"end_time": "17:30:00",
			}
		}
		attendance = [
			{
				"course_session": "CS-001",
				"student": "STU-001",
				"enrollment_type": "Trial",
				"status": "To be started",
				"source_doctype": "Inquiry",
				"source_document": "INQ-001",
			},
			{
				"course_session": "CS-001",
				"student": "STU-001",
				"enrollment_type": "Trial",
				"status": "To be started",
				"source_doctype": "Inquiry",
				"source_document": "INQ-001",
			},
		]
		inquiries = {
			"INQ-001": {
				"name": "INQ-001",
				"inquiry_type": "Trial Lesson",
				"status": "Booked",
				"course_session": "CS-001",
				"student": "STU-001",
				"trial_invoice": None,
			}
		}

		rows = _build_eligible_trial_rows(
			sessions,
			timeslots,
			attendance,
			inquiries,
			{"STU-001": "Ava Test"},
			{},
		)
		self.assertEqual(len(rows), 1)

	def test_assigns_only_campuses_visible_to_each_admin(self):
		trials = [
			trial_row("Indooroopilly", inquiry="INQ-I"),
			trial_row("Upper Mount Gravatt", inquiry="INQ-U"),
		]
		recipients = [
			{
				"profile": "CAP-I",
				"user": "indoor@example.com",
				"email": "indoor@example.com",
				"campuses": ["Indooroopilly"],
			},
			{
				"profile": "CAP-BOTH",
				"user": "both@example.com",
				"email": "both@example.com",
				"campuses": ["Indooroopilly", "Upper Mount Gravatt"],
			},
		]
		groups = _assign_trials_to_recipients(trials, recipients)

		self.assertEqual([row["inquiry"] for row in groups["CAP-I"]["trials"]], ["INQ-I"])
		self.assertEqual(
			[row["inquiry"] for row in groups["CAP-BOTH"]["trials"]],
			["INQ-I", "INQ-U"],
		)

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._", side_effect=lambda value: value)
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.formatdate",
		return_value="Saturday 25 July 2026",
	)
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.fmt_money", return_value="$20.00")
	def test_message_shows_trial_count_payment_states_and_reception_instruction(
		self,
		_mock_money,
		_mock_formatdate,
		_mock_translate,
	):
		message = _digest_message(
			"2026-07-25",
			[
				trial_row(payment_status="Paid", outstanding_amount=0),
				trial_row(
					inquiry="INQ-002",
					student_name="Ben Test",
					payment_status="Partially Paid",
					outstanding_amount=20,
				),
				trial_row(
					"Upper Mount Gravatt",
					inquiry="INQ-003",
					student_name="Cara Test",
					payment_status="Payment Required at Reception",
					outstanding_amount=None,
				),
			],
			admin_name="Campus Manager",
		)

		self.assertIn("There are 3 Trial students tomorrow.", message)
		self.assertIn("Hi Campus Manager,", message)
		self.assertIn("Indooroopilly", message)
		self.assertIn("Upper Mount Gravatt", message)
		self.assertIn("Ava Test", message)
		self.assertIn("Paid", message)
		self.assertIn("Partially Paid", message)
		self.assertIn("$20.00", message)
		self.assertIn("Payment Required at Reception", message)
		self.assertIn("Please collect payment at reception", message)
		self.assertNotIn("No Invoice", message)

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._", side_effect=lambda value: value)
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.formatdate",
		return_value="Saturday 25 July 2026",
	)
	def test_subject_identifies_tomorrow_trial_students(self, _mock_formatdate, _mock_translate):
		self.assertEqual(
			_digest_subject("2026-07-25"),
			"Tomorrow's Trial students — Saturday 25 July 2026",
		)

	def test_event_key_is_stable_per_profile_and_date(self):
		self.assertEqual(
			campus_admin_trial_digest_event_key("CAP-001", "2026-07-25"),
			campus_admin_trial_digest_event_key("CAP-001", "2026-07-25"),
		)
		self.assertNotEqual(
			campus_admin_trial_digest_event_key("CAP-001", "2026-07-25"),
			campus_admin_trial_digest_event_key("CAP-001", "2026-07-26"),
		)

	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.enqueue_campus_admin_next_day_trial_digests"
	)
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.get_datetime_in_timezone")
	def test_run_uses_brisbane_tomorrow_at_7_pm(self, mock_now, mock_enqueue):
		mock_now.return_value = SimpleNamespace(hour=19, date=lambda: date(2026, 7, 24))
		mock_enqueue.return_value = {"queued": 1}

		result = run_campus_admin_next_day_trial_digests()

		mock_enqueue.assert_called_once_with(date(2026, 7, 25))
		self.assertEqual(result, {"queued": 1})

	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.enqueue_campus_admin_next_day_trial_digests"
	)
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.get_datetime_in_timezone")
	def test_run_skips_outside_7_pm_brisbane(self, mock_now, mock_enqueue):
		mock_now.return_value = SimpleNamespace(hour=18)

		result = run_campus_admin_next_day_trial_digests()

		self.assertTrue(result["skipped"])
		mock_enqueue.assert_not_called()

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._mark_notification_queued")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest._create_notification_log",
		side_effect=["LOG-1", "LOG-2"],
	)
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.outbound_email_enabled", return_value=True
	)
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest._notification_event_exists",
		return_value=False,
	)
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.frappe.enqueue")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.get_campus_admin_next_day_trial_groups"
	)
	def test_enqueue_creates_one_job_per_admin(
		self,
		mock_groups,
		mock_enqueue,
		_mock_exists,
		_mock_outbound,
		_mock_create_log,
		mock_mark_queued,
	):
		mock_groups.return_value = {
			"CAP-1": {
				"recipient": {
					"profile": "CAP-1",
					"user": "one@example.com",
					"email": "one@example.com",
					"display_name": "One",
				},
				"trials": [trial_row()],
			},
			"CAP-2": {
				"recipient": {
					"profile": "CAP-2",
					"user": "two@example.com",
					"email": "two@example.com",
					"display_name": "Two",
				},
				"trials": [trial_row()],
			},
		}

		result = enqueue_campus_admin_next_day_trial_digests("2026-07-25")

		self.assertEqual(result["queued"], 2)
		self.assertEqual(mock_enqueue.call_count, 2)
		self.assertEqual(mock_mark_queued.call_count, 2)
		self.assertEqual(
			{call.kwargs["profile"] for call in mock_enqueue.call_args_list},
			{"CAP-1", "CAP-2"},
		)

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._create_notification_log")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest._notification_event_exists",
		return_value=True,
	)
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.frappe.enqueue")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.get_campus_admin_next_day_trial_groups"
	)
	def test_enqueue_skips_existing_profile_date_event(
		self,
		mock_groups,
		mock_enqueue,
		_mock_exists,
		mock_create_log,
	):
		mock_groups.return_value = {
			"CAP-1": {
				"recipient": {"profile": "CAP-1", "email": "one@example.com"},
				"trials": [trial_row()],
			}
		}

		result = enqueue_campus_admin_next_day_trial_digests("2026-07-25")

		self.assertEqual(result["queued"], 0)
		self.assertEqual(result["skipped"], 1)
		mock_enqueue.assert_not_called()
		mock_create_log.assert_not_called()

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._mark_notification_failed")
	@patch("qas_custom.modules.notifications.campus_admin_trial_digest.sendmail_or_skip")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.get_campus_admin_next_day_trial_groups"
	)
	def test_send_job_rechecks_and_skips_when_no_trial_remains(self, mock_groups, mock_send, mock_failed):
		mock_groups.return_value = {}

		result = send_campus_admin_next_day_trial_digest_job(
			"CAP-001",
			"2026-07-25",
			notification_log="LOG-001",
		)

		self.assertTrue(result["skipped"])
		mock_send.assert_not_called()
		mock_failed.assert_called_once_with("LOG-001", "No eligible next-day Trial students remain.")

	@patch("qas_custom.modules.notifications.campus_admin_trial_digest._mark_notification_sent")
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.sendmail_or_skip",
		return_value={"skipped": False},
	)
	@patch(
		"qas_custom.modules.notifications.campus_admin_trial_digest.get_campus_admin_next_day_trial_groups"
	)
	def test_send_job_rechecks_profile_assignments_before_sending(self, mock_groups, mock_send, mock_sent):
		mock_groups.return_value = {
			"CAP-001": {
				"recipient": {
					"profile": "CAP-001",
					"user": "admin@example.com",
					"email": "admin@example.com",
					"display_name": "Campus Admin",
				},
				"trials": [trial_row()],
			}
		}

		result = send_campus_admin_next_day_trial_digest_job(
			"CAP-001",
			"2026-07-25",
			notification_log="LOG-001",
		)

		mock_groups.assert_called_once_with("2026-07-25", profile="CAP-001")
		self.assertTrue(result["sent"])
		self.assertEqual(result["trial_count"], 1)
		self.assertEqual(mock_send.call_args.kwargs["recipients"], ["admin@example.com"])
		mock_sent.assert_called_once_with("LOG-001")
