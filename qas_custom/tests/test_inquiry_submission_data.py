from unittest import TestCase

from qas_custom.services.inquiry import _submission_data_rows


class TestInquirySubmissionData(TestCase):
	def test_formats_form_values_and_hides_technical_data(self):
		rows = _submission_data_rows(
			{
				"parent_name": {"first_name": "Kylie", "last_name": "Keioskie"},
				"student_name": {"first_name": "Mya"},
				"student_dob": "05/23/2013",
				"preferred_days": ["Monday", "Wednesday"],
				"has_medical_note": False,
				"webhook_token": "must-not-be-returned",
				"nested": {"csrf_token": "also-hidden", "visible_answer": "Shown"},
				"_submission": {"browser": "Chrome"},
			}
		)

		self.assertEqual(
			rows,
			[
				{"label": "parent name · first name", "value": "Kylie"},
				{"label": "parent name · last name", "value": "Keioskie"},
				{"label": "student name · first name", "value": "Mya"},
				{"label": "student dob", "value": "05/23/2013"},
				{"label": "preferred days", "value": "Monday, Wednesday"},
				{"label": "has medical note", "value": "No"},
				{"label": "nested · visible answer", "value": "Shown"},
			],
		)

	def test_returns_no_rows_for_missing_or_invalid_payload(self):
		self.assertEqual(_submission_data_rows(None), [])
		self.assertEqual(_submission_data_rows("not-json"), [])
