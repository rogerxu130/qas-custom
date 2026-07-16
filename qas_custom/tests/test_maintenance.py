from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from qas_custom.services import maintenance


class TestInquiryAttendanceMaintenance(TestCase):
	def _find_cancelled_inquiry_issues(self, attendance_entries):
		inquiry = SimpleNamespace(
			name="INQ-0001",
			status="Cancelled",
			student="STU-0001",
			course_session="SESSION-0001",
		)
		with (
			patch.object(maintenance, "_doctype_available", return_value=True),
			patch.object(maintenance.frappe, "get_all", return_value=[inquiry]),
			patch.object(
				maintenance,
				"_get_attendance_by_source",
				return_value={inquiry.name: attendance_entries},
			),
			patch.object(maintenance, "_", side_effect=lambda value: value),
		):
			return list(maintenance._find_inquiry_attendance_issues())

	def test_cancelled_inquiry_with_only_cancelled_attendance_has_no_issue(self):
		issues = self._find_cancelled_inquiry_issues(
			[
				SimpleNamespace(
					name="ATT-0001",
					student="STU-0001",
					course_session="OLD-SESSION",
					status="Cancelled",
				)
			]
		)

		self.assertEqual(issues, [])

	def test_cancelled_inquiry_with_active_attendance_reports_warning(self):
		issues = self._find_cancelled_inquiry_issues(
			[
				SimpleNamespace(
					name="ATT-CANCELLED",
					student="STU-0001",
					course_session="SESSION-0001",
					status="Cancelled",
				),
				SimpleNamespace(
					name="ATT-ACTIVE",
					student="STU-0001",
					course_session="SESSION-0001",
					status="To be started",
				),
			]
		)

		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0]["severity"], "Warning")
		self.assertEqual(issues[0]["related_document"], "ATT-ACTIVE")
