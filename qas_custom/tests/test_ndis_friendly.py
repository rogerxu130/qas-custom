from unittest import TestCase
from types import SimpleNamespace
from unittest.mock import Mock, patch

from qas_custom.services import ndis_friendly


class TestNdisFriendlyCapacity(TestCase):
	@patch("qas_custom.services.maintenance._notify_school_admins_of_new_issues")
	@patch("qas_custom.services.maintenance._upsert_data_issue", return_value=("QDI-1", False))
	@patch("qas_custom.services.maintenance._doctype_available", return_value=True)
	def test_reopened_issue_notifies_only_when_requested(self, _mock_doctype, _mock_upsert, mock_notify):
		fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=Mock(return_value="Resolved"), commit=Mock()))
		issue = {"issue_key": "ndis-friendly-capacity:WT-NDIS"}

		with patch("qas_custom.services.maintenance.frappe", fake_frappe):
			result = ndis_friendly.record_data_issue(issue)
			self.assertTrue(result["reopened"])
			mock_notify.assert_not_called()

			result = ndis_friendly.record_data_issue(issue, notify_on_reopen=True)

			self.assertTrue(result["reopened"])
		mock_notify.assert_called_once_with(["QDI-1"])

	@patch("qas_custom.services.ndis_friendly._has_field", return_value=True)
	@patch("qas_custom.services.ndis_friendly._doctype_available", return_value=True)
	@patch("qas_custom.services.ndis_friendly.frappe.get_all")
	def test_counts_unique_planned_and_active_students_only(self, mock_get_all, _mock_doctype, _mock_has_field):
		mock_get_all.side_effect = [
			[
				{
					"name": "WT-NDIS",
					"ndis_friendly": 1,
					"ndis_public_listing_enabled": 1,
				}
			],
			[
				{"weekly_timeslot": "WT-NDIS", "student": "STU-1"},
				{"weekly_timeslot": "WT-NDIS", "student": "STU-1"},
				{"weekly_timeslot": "WT-NDIS", "student": "STU-2"},
				{"weekly_timeslot": "WT-NDIS", "student": "STU-3"},
				{"weekly_timeslot": "WT-NDIS", "student": "STU-4"},
			],
		]

		status = ndis_friendly.get_ndis_friendly_capacity_status("WT-NDIS")

		self.assertTrue(status["ndis_friendly"])
		self.assertTrue(status["ndis_public_listing_enabled"])
		self.assertEqual(status["ndis_enrollment_count"], 4)
		self.assertTrue(status["ndis_capacity_reached"])
		self.assertFalse(status["ndis_capacity_exceeded"])

	@patch("qas_custom.services.ndis_friendly.record_data_issue", return_value={"issue": "QDI-1", "created": True})
	@patch("qas_custom.services.ndis_friendly.get_ndis_friendly_capacity_status")
	@patch("qas_custom.services.ndis_friendly._", side_effect=lambda value: value)
	@patch("qas_custom.services.ndis_friendly._set_ndis_capacity_alert_active")
	@patch("qas_custom.services.ndis_friendly._data_issue_status", return_value=None)
	def test_opens_one_capacity_issue_when_limit_is_reached(
		self, _mock_issue_status, mock_set_alert_active, _mock_translate, mock_status, mock_record
	):
		mock_status.return_value = {
			"ndis_friendly": True,
			"ndis_enrollment_count": 4,
			"ndis_capacity": 4,
			"ndis_capacity_reached": True,
			"ndis_capacity_exceeded": False,
		}

		result = ndis_friendly.refresh_ndis_friendly_capacity_alert("WT-NDIS")

		self.assertTrue(result["issue_created"])
		self.assertEqual(result["issue"], "QDI-1")
		issue = mock_record.call_args.args[0]
		self.assertEqual(issue["issue_key"], "ndis-friendly-capacity:WT-NDIS")
		self.assertEqual(issue["issue_type"], "NDIS Capacity")
		self.assertEqual(issue["severity"], "Warning")
		self.assertIn("Show on public NDIS listing", issue["suggested_action"])
		self.assertTrue(mock_record.call_args.kwargs["notify_on_reopen"])
		mock_set_alert_active.assert_called_once_with("WT-NDIS", True)

	@patch("qas_custom.services.ndis_friendly.record_data_issue")
	@patch("qas_custom.services.ndis_friendly._data_issue_status", return_value="Ignored")
	@patch("qas_custom.services.ndis_friendly.get_ndis_friendly_capacity_status")
	def test_dismissed_current_capacity_alert_stays_suppressed(self, mock_status, _mock_issue_status, mock_record):
		mock_status.return_value = {
			"ndis_friendly": True,
			"ndis_enrollment_count": 4,
			"ndis_capacity": 4,
			"ndis_capacity_reached": True,
			"ndis_capacity_exceeded": False,
			"ndis_capacity_alert_active": True,
		}

		result = ndis_friendly.refresh_ndis_friendly_capacity_alert("WT-NDIS")

		self.assertTrue(result["issue_acknowledged"])
		mock_record.assert_not_called()

	@patch("qas_custom.services.ndis_friendly.resolve_data_issue", return_value=True)
	@patch("qas_custom.services.ndis_friendly.get_ndis_friendly_capacity_status")
	@patch("qas_custom.services.ndis_friendly._set_ndis_capacity_alert_active")
	def test_resolves_alert_when_roster_drops_below_limit(self, mock_set_alert_active, mock_status, mock_resolve):
		mock_status.return_value = {
			"ndis_friendly": True,
			"ndis_enrollment_count": 3,
			"ndis_capacity": 4,
			"ndis_capacity_reached": False,
			"ndis_capacity_exceeded": False,
			"ndis_capacity_alert_active": True,
		}

		fake_frappe = SimpleNamespace(db=SimpleNamespace(commit=Mock()))
		with patch("qas_custom.services.ndis_friendly.frappe", fake_frappe):
			result = ndis_friendly.refresh_ndis_friendly_capacity_alert("WT-NDIS")

		self.assertTrue(result["issue_resolved"])
		mock_resolve.assert_called_once_with("ndis-friendly-capacity:WT-NDIS")
		mock_set_alert_active.assert_called_once_with("WT-NDIS", False)
		fake_frappe.db.commit.assert_called_once()
