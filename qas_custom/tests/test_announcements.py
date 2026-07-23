from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.announcements import (
	ANNOUNCEMENT_VISIBLE_RECIPIENT,
	_announcement_student_preview,
	_message_html,
	_resolve_announcement_recipients,
	_send_announcement_bcc_batch,
	_student_search_rank,
	_validate_announcement,
	search_school_admin_announcement_students_data,
	send_school_announcement_email_job,
)


class TestSingleStudentAnnouncements(TestCase):
	def test_message_html_linkifies_plain_urls(self):
		result = _message_html("Read the guide:\nhttps://example.com/guide")

		self.assertIn('<a href="https://example.com/guide"', result)
		self.assertIn("<br>", result)

	def test_message_html_removes_unsafe_markup(self):
		result = _message_html('<p>Hello</p><script>alert(1)</script><a href="javascript:alert(2)">Bad</a>')

		self.assertIn("<p>Hello</p>", result)
		self.assertNotIn("<script", result)
		self.assertNotIn("javascript:", result)

	def test_single_student_audience_requires_student(self):
		doc = frappe._dict(title="Test", body="Message", audience_type="Single Student", student="")

		with self.assertRaises(frappe.ValidationError):
			_validate_announcement(doc)

	@patch("qas_custom.services.announcements._recipient_from_parent_name")
	@patch("qas_custom.services.announcements._student_parent", return_value="PAR-1")
	def test_single_student_resolves_exactly_one_family(self, _parent, recipient_from_parent):
		recipient_from_parent.return_value = {
			"parent": "PAR-1",
			"student": "STU-1",
			"email": "parent@example.com",
		}
		db = SimpleNamespace(exists=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			result = _resolve_announcement_recipients(
				frappe._dict(audience_type="Single Student", student="STU-1")
			)

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["parent"], "PAR-1")
		recipient_from_parent.assert_called_once_with(
			"PAR-1",
			student="STU-1",
			audience_source="Single Student",
			source_document="STU-1",
		)

	@patch("qas_custom.services.announcements._student_parent", return_value=None)
	def test_single_student_without_parent_is_blocked(self, _parent):
		db = SimpleNamespace(exists=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			with self.assertRaises(frappe.ValidationError):
				_resolve_announcement_recipients(
					frappe._dict(audience_type="Single Student", student="STU-1")
				)

	def test_student_search_prefers_exact_then_prefix_matches(self):
		rows = [
			frappe._dict(name="STU-3", student_name="Alexa Young"),
			frappe._dict(name="STU-2", student_name="Alexandra Young"),
			frappe._dict(name="STU-1", student_name="Alex"),
		]

		ordered = sorted(rows, key=lambda row: _student_search_rank(row, "Alex"))

		self.assertEqual([row.name for row in ordered], ["STU-1", "STU-3", "STU-2"])

	@patch("qas_custom.services.announcements._announcement_student_preview", side_effect=lambda row: {"student": row.name})
	@patch("qas_custom.services.announcements._student_search_fields", return_value=["name", "student_name"])
	@patch("qas_custom.services.announcements._require_school_admin")
	@patch("qas_custom.services.announcements.frappe.get_all")
	def test_server_search_combines_exact_prefix_and_contains_results(self, get_all, _require, _fields, _preview):
		get_all.side_effect = [
			[frappe._dict(name="STU-1", student_name="Alex")],
			[frappe._dict(name="STU-2", student_name="Alexa")],
			[frappe._dict(name="STU-3", student_name="Sam Alex")],
		]

		result = search_school_admin_announcement_students_data("Alex", 20)

		self.assertEqual([row["student"] for row in result["items"]], ["STU-1", "STU-2", "STU-3"])
		self.assertEqual(get_all.call_count, 3)

	def test_student_preview_uses_family_identity_and_email(self):
		parent = frappe._dict(
			name="PAR-1",
			parent_name="Parent One",
			customer="CUS-1",
			linked_user="parent@example.com",
			email="parent@example.com",
		)
		db = SimpleNamespace(get_value=Mock(return_value=parent), has_column=Mock(return_value=True))
		with patch("qas_custom.services.announcements.frappe.db", db):
			result = _announcement_student_preview(
				frappe._dict(
					name="STU-1",
					student_name="Student One",
					guardian="PAR-1",
					student_code="S001",
					status="Active",
				)
			)

		self.assertTrue(result["eligible"])
		self.assertEqual(result["parent"], "PAR-1")
		self.assertEqual(result["parent_name"], "Parent One")
		self.assertEqual(result["parent_email"], "parent@example.com")
		self.assertEqual(result["customer"], "CUS-1")


class TestAnnouncementBccEmailDelivery(TestCase):
	def _run_job(self, rows, send_side_effect=None):
		doc = frappe._dict(
			name="ANN-2026-00001",
			title="Term update",
			email_subject="Term update email",
		)
		db = SimpleNamespace(
			exists=Mock(return_value=True),
			set_value=Mock(),
			commit=Mock(),
		)
		fake_frappe = SimpleNamespace(
			db=db,
			get_doc=Mock(return_value=doc),
			get_all=Mock(return_value=rows),
			get_traceback=Mock(return_value="traceback"),
			log_error=Mock(),
		)
		send_batch = Mock(side_effect=send_side_effect)
		if send_side_effect is None:
			send_batch.return_value = {"sent": len(rows)}
		with patch("qas_custom.services.announcements.frappe", fake_frappe), patch(
			"qas_custom.services.announcements._send_announcement_bcc_batch",
			send_batch,
		), patch(
			"qas_custom.services.announcements._announcement_email_message",
			return_value="<p>Term update</p>",
		), patch(
			"qas_custom.services.announcements.now_datetime",
			return_value="2026-07-22 12:00:00",
		):
			result = send_school_announcement_email_job(doc.name)
		return result, send_batch, db, fake_frappe

	def test_queued_emails_are_split_into_bcc_batches_of_fifty(self):
		rows = [
			frappe._dict(name=f"ANR-{index:03d}", email=f"parent{index:03d}@example.com")
			for index in range(120)
		]

		result, send_batch, db, _fake_frappe = self._run_job(rows)

		self.assertEqual([len(call.kwargs["bcc"]) for call in send_batch.call_args_list], [50, 50, 20])
		for call in send_batch.call_args_list:
			self.assertEqual(call.kwargs["subject"], "Term update email")
			self.assertEqual(call.kwargs["reference_name"], "ANN-2026-00001")
		self.assertEqual(result, {"sent": 120, "failed": 0})
		self.assertEqual(db.set_value.call_count, 3)
		self.assertEqual(db.commit.call_count, 3)

	def test_duplicate_addresses_are_bcced_once_and_all_rows_are_updated(self):
		rows = [
			frappe._dict(name="ANR-001", email=" Parent@Example.com "),
			frappe._dict(name="ANR-002", email="parent@example.com"),
			frappe._dict(name="ANR-003", email=""),
		]

		result, send_batch, db, _fake_frappe = self._run_job(rows)

		self.assertEqual(send_batch.call_count, 1)
		self.assertEqual(send_batch.call_args.kwargs["bcc"], ["Parent@Example.com"])
		missing_call, sent_call = db.set_value.call_args_list
		self.assertEqual(missing_call.args[1], {"name": ["in", ["ANR-003"]]})
		self.assertEqual(missing_call.args[2]["email_status"], "Failed")
		self.assertEqual(sent_call.args[1], {"name": ["in", ["ANR-001", "ANR-002"]]})
		self.assertEqual(sent_call.args[2]["email_status"], "Sent")
		self.assertEqual(result, {"sent": 2, "failed": 1})

	def test_failed_bcc_batch_does_not_block_later_batches(self):
		rows = [
			frappe._dict(name=f"ANR-{index:03d}", email=f"parent{index:03d}@example.com")
			for index in range(60)
		]

		result, send_batch, db, fake_frappe = self._run_job(
			rows,
			send_side_effect=[RuntimeError("SMTP batch failed"), {"sent": 10}],
		)

		self.assertEqual(send_batch.call_count, 2)
		failed_call, sent_call = db.set_value.call_args_list
		self.assertEqual(failed_call.args[2]["email_status"], "Failed")
		self.assertEqual(len(failed_call.args[1]["name"][1]), 50)
		self.assertEqual(sent_call.args[2]["email_status"], "Sent")
		self.assertEqual(len(sent_call.args[1]["name"][1]), 10)
		self.assertEqual(result, {"sent": 10, "failed": 50})
		fake_frappe.log_error.assert_called_once()
		self.assertEqual(db.commit.call_count, 2)

	@patch("qas_custom.services.announcements.outbound_email_enabled", return_value=True)
	@patch("qas_custom.services.announcements.QueueBuilder")
	def test_true_bcc_batch_uses_one_smtp_transaction(self, queue_builder, _email_enabled):
		mail = SimpleNamespace(
			bcc=["parent1@example.com", "parent2@example.com"],
			sender="Queensland Art School <queenslandartschool@gmail.com>",
			set_message_id=Mock(),
			as_string=Mock(
				return_value=(
					"From: Queensland Art School <queenslandartschool@gmail.com>\r\n"
					f"To: {ANNOUNCEMENT_VISIBLE_RECIPIENT}\r\n"
					"Subject: Term update\r\n\r\nMessage"
				)
			),
		)
		smtp_server = SimpleNamespace(
			session=SimpleNamespace(sendmail=Mock()),
			quit=Mock(),
		)
		email_account = SimpleNamespace(
			append_emails_to_sent_folder=1,
			append_email_to_sent_folder=Mock(),
			get_smtp_server=Mock(return_value=smtp_server),
		)
		builder = SimpleNamespace(
			get_outgoing_email_account=Mock(return_value=email_account),
			prepare_email_content=Mock(return_value=mail),
		)
		queue_builder.return_value = builder

		result = _send_announcement_bcc_batch(
			bcc=mail.bcc,
			subject="Term update",
			message="<p>Message</p>",
			reference_doctype="School Announcement",
			reference_name="ANN-2026-00001",
		)

		queue_builder.assert_called_once_with(
			recipients=[ANNOUNCEMENT_VISIBLE_RECIPIENT],
			bcc=mail.bcc,
			subject="Term update",
			message="<p>Message</p>",
			reference_doctype="School Announcement",
			reference_name="ANN-2026-00001",
			expose_recipients="header",
			add_unsubscribe_link=0,
		)
		smtp_server.session.sendmail.assert_called_once()
		send_call = smtp_server.session.sendmail.call_args.kwargs
		self.assertEqual(
			send_call["to_addrs"],
			[
				ANNOUNCEMENT_VISIBLE_RECIPIENT,
				"parent1@example.com",
				"parent2@example.com",
			],
		)
		self.assertNotIn(b"Bcc:", send_call["msg"])
		email_account.append_email_to_sent_folder.assert_called_once_with(send_call["msg"])
		smtp_server.quit.assert_called_once()
		self.assertEqual(result, {"sent": 2})
