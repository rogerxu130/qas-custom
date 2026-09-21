from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.term4_class_id_migration import SUPPORTED_TERM, preview


class TestTerm4ClassIdMigration(TestCase):
	def test_preview_is_read_only_and_reports_duplicate(self):
		db = Mock()
		frappe_api = Mock()
		frappe_api.session.user = "Administrator"
		slots = [
			frappe._dict(name="Term 4 2026-Course A-Indooroopilly-Saturday-14:00:00-Jayson", modified="1", status="Inactive", course="Course A", class_language="English", campus="Indooroopilly", classroom="R4", day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00"),
			frappe._dict(name="WTS-2026-00009", modified="2", status="Active", course="Course A", class_language="English", campus="Indooroopilly", classroom="R4", day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00"),
		]
		sessions = [frappe._dict(name="legacy-session", weekly_timeslot=slots[0].name, session_date="2026-10-10", status="Scheduled", modified="3")]
		with patch("qas_custom.services.term4_class_id_migration._require_access"), patch("qas_custom.services.term4_class_id_migration.frappe.get_all", side_effect=[slots, sessions]), patch("qas_custom.services.term4_class_id_migration.frappe.db", new=db):
			result = preview(SUPPORTED_TERM)
		self.assertEqual(result["weekly_timeslot_mapping"][slots[0].name], "WTS-2026-00010")
		self.assertEqual(result["course_session_mapping"]["legacy-session"], "CS-2026-00001")
		self.assertEqual(result["duplicate_groups"][0]["records"], [slots[0].name, "WTS-2026-00009"])
		self.assertTrue(result["blocking_errors"])
		db.sql.assert_not_called()
		db.commit.assert_not_called()
