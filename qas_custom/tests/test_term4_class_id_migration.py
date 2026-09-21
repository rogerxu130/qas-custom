from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from qas_custom.services.term4_class_id_migration import (
	SUPPORTED_TERM,
	_link_fields,
	execute_duplicate_consolidation,
	preview,
	preview_duplicate_consolidation,
)


class TestTerm4ClassIdMigration(TestCase):
	@patch("qas_custom.services.term4_class_id_migration.frappe.get_all")
	def test_link_fields_reads_standard_and_custom_owners_and_deduplicates(self, get_all):
		get_all.side_effect = [
			[
				frappe._dict(parent="Enrollment", fieldname="weekly_timeslot"),
				frappe._dict(parent="Course Sessions", fieldname="weekly_timeslot"),
			],
			[
				frappe._dict(dt="Enrollment", fieldname="weekly_timeslot"),
				frappe._dict(dt="Enrollment", fieldname="custom_original_timeslot"),
			],
		]

		self.assertEqual(_link_fields("Weekly Timeslot"), [
			("Course Sessions", "weekly_timeslot"),
			("Enrollment", "custom_original_timeslot"),
			("Enrollment", "weekly_timeslot"),
		])
		self.assertEqual(get_all.call_args_list, [
			call("DocField", filters={"fieldtype": "Link", "options": "Weekly Timeslot"}, fields=["parent", "fieldname"], limit_page_length=0),
			call("Custom Field", filters={"fieldtype": "Link", "options": "Weekly Timeslot"}, fields=["dt", "fieldname"], limit_page_length=0),
		])

	def test_preview_is_read_only_and_reports_duplicate(self):
		db = Mock()
		frappe_api = Mock()
		frappe_api.session.user = "Administrator"
		slots = [
			frappe._dict(name="Term 4 2026-Course A-Indooroopilly-Saturday-14:00:00-Jayson", modified="1", status="Inactive", course="Course A", class_language="English", campus="Indooroopilly", classroom="R4", day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00"),
			frappe._dict(name="WTS-2026-00009", modified="2", status="Active", course="Course A", class_language="English", campus="Indooroopilly", classroom="R4", day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00"),
		]
		sessions = [frappe._dict(name="legacy-session", weekly_timeslot=slots[0].name, session_date="2026-10-10", status="Scheduled", modified="3")]
		with patch("qas_custom.services.term4_class_id_migration._require_access"), patch("qas_custom.services.term4_class_id_migration._reference_inventory", return_value=[]), patch("qas_custom.services.term4_class_id_migration.frappe.get_all", side_effect=[slots, sessions]), patch("qas_custom.services.term4_class_id_migration.frappe.db", new=db):
			result = preview(SUPPORTED_TERM)
		self.assertEqual(result["weekly_timeslot_mapping"][slots[0].name], "WTS-2026-00010")
		self.assertEqual(result["course_session_mapping"]["legacy-session"], "CS-2026-00001")
		self.assertEqual(result["duplicate_groups"][0]["records"], [slots[0].name, "WTS-2026-00009"])
		self.assertTrue(result["blocking_errors"])
		db.sql.assert_not_called()
		db.commit.assert_not_called()

	@patch("qas_custom.services.term4_class_id_migration._require_access")
	@patch("qas_custom.services.term4_class_id_migration._reference_inventory", return_value=[])
	@patch("qas_custom.services.term4_class_id_migration.frappe.get_all")
	@patch("qas_custom.services.term4_class_id_migration.frappe.get_doc")
	def test_duplicate_preview_pairs_same_date_sessions_and_is_read_only(self, get_doc, get_all, _inventory, _access):
		common = dict(term=SUPPORTED_TERM, course="Course A", class_language="English", campus="Indooroopilly", classroom="R4", day_of_week="Saturday", start_time="14:00:00", end_time="15:30:00", modified="1")
		get_doc.side_effect = [frappe._dict(name="LONG-WTS", **common), frappe._dict(name="WTS-2026-00009", **common)]
		get_all.side_effect = [
			[frappe._dict(name="LONG-CS", weekly_timeslot="LONG-WTS", session_date="2026-10-10", status="Scheduled", modified="1")],
			[
				frappe._dict(name="SHORT-CS-1", weekly_timeslot="WTS-2026-00009", session_date="2026-10-10", status="Scheduled", modified="2"),
				frappe._dict(name="SHORT-CS-2", weekly_timeslot="WTS-2026-00009", session_date="2026-10-17", status="Scheduled", modified="3"),
			],
			[],
		]

		result = preview_duplicate_consolidation("LONG-WTS", "WTS-2026-00009")

		self.assertEqual([row["action"] for row in result["session_actions"]], ["merge", "rebind"])
		self.assertFalse(result["blocking_errors"])
		self.assertTrue(result["confirmation_token"])

	@patch("qas_custom.services.term4_class_id_migration._require_access")
	@patch("qas_custom.services.term4_class_id_migration._reference_inventory", return_value=[])
	@patch("qas_custom.services.term4_class_id_migration.preview_duplicate_consolidation")
	def test_duplicate_execute_uses_supported_merges_and_preserves_invoice_snapshot(self, preview_consolidation, _inventory, _access):
		preview_consolidation.return_value = {
			"blocking_errors": [], "confirmation_token": "token",
			"session_actions": [
				{"action": "merge", "source": "SHORT-CS-1", "target": "LONG-CS"},
				{"action": "rebind", "source": "SHORT-CS-2", "target_weekly_timeslot": "LONG-WTS"},
			],
			"invoice_snapshot": [{"enrollment": "ENR-1", "invoice": "INV-1", "invoice_status": "Draft", "invoice_amount": 100}],
		}
		db = Mock()
		db.exists.return_value = None
		db.get_value.return_value = frappe._dict(invoice="INV-1", invoice_status="Draft", invoice_amount=100)
		with patch("qas_custom.services.term4_class_id_migration.frappe.db", new=db), patch("qas_custom.services.term4_class_id_migration.frappe.rename_doc") as rename_doc:
			result = execute_duplicate_consolidation("LONG-WTS", "WTS-2026-00009", "token")

		rename_doc.assert_any_call("Course Sessions", "SHORT-CS-1", "LONG-CS", force=True, merge=True, ignore_permissions=True)
		rename_doc.assert_any_call("Weekly Timeslot", "WTS-2026-00009", "LONG-WTS", force=True, merge=True, ignore_permissions=True)
		db.set_value.assert_called_once_with("Course Sessions", "SHORT-CS-2", "weekly_timeslot", "LONG-WTS", update_modified=True)
		self.assertEqual(result["invoice_snapshot"][0]["invoice"], "INV-1")
		self.assertTrue(result["ok"])
