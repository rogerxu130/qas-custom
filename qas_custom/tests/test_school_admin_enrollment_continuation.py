from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import continue_school_admin_enrollment_to_term_data


MODULE = "qas_custom.services.school_admin"


class FakeDocument:
	def __init__(self, name, **values):
		self.name = name
		self.values = values
		self.insert = Mock()

	def get(self, fieldname, default=None):
		return self.values.get(fieldname, default)


class TestSchoolAdminEnrollmentContinuation(TestCase):
	def source(self):
		return FakeDocument(
			"ENR-SOURCE",
			student="STU-1",
			parent="PAR-1",
			term="TERM-3",
			course="COURSE-1",
			weekly_timeslot="WT-SOURCE",
			enrollment_type="Full-Term",
			status="Active",
		)

	def target_term(self):
		return {"name": "TERM-4", "status": "Upcoming", "start_date": "2026-10-01"}

	def target_timeslot(self, course="COURSE-1"):
		return {"name": "WT-TARGET", "term": "TERM-4", "course": course, "status": "Active"}

	@patch(MODULE + "._refresh_ndis_friendly_capacity_alerts")
	@patch(MODULE + "._build_enrollment_payload")
	@patch(MODULE + "._add_comment")
	@patch(MODULE + "._validate_unique_open_enrollment")
	@patch(MODULE + "._apply_enrollment_payload")
	@patch(MODULE + "._existing_target_enrollment", return_value=None)
	@patch(MODULE + "._require_school_admin")
	def test_creates_planned_enrollment_without_changing_source(
		self,
		_require,
		_existing,
		apply_payload,
		validate,
		add_comment,
		build_payload,
		_refresh,
	):
		source = self.source()
		target = FakeDocument("ENR-TARGET")
		build_payload.return_value = {"name": target.name, "status": "Planned"}
		with patch(MODULE + ".frappe") as frappe_mock, patch(MODULE + "._", side_effect=lambda value: value):
			frappe_mock.get_doc.return_value = source
			frappe_mock.new_doc.return_value = target
			frappe_mock.db.get_value.side_effect = [self.target_term(), self.target_timeslot()]
			result = continue_school_admin_enrollment_to_term_data(
				enrollment=source.name,
				payload={"term": "TERM-4", "weekly_timeslot": "WT-TARGET"},
			)

		self.assertTrue(result["created"])
		self.assertEqual(source.get("term"), "TERM-3")
		self.assertEqual(source.get("weekly_timeslot"), "WT-SOURCE")
		payload = apply_payload.call_args.args[1]
		self.assertEqual(payload["term"], "TERM-4")
		self.assertEqual(payload["weekly_timeslot"], "WT-TARGET")
		self.assertEqual(payload["status"], "Planned")
		self.assertEqual(payload["enrollment_date"], "2026-10-01")
		validate.assert_called_once_with(target)
		target.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(add_comment.call_count, 2)
		frappe_mock.db.sql.assert_called_once()
		frappe_mock.db.commit.assert_called_once()

	@patch(MODULE + "._build_enrollment_payload", return_value={"name": "ENR-EXISTING"})
	@patch(MODULE + "._existing_target_enrollment", return_value="ENR-EXISTING")
	@patch(MODULE + "._require_school_admin")
	def test_returns_existing_open_enrollment_instead_of_creating_duplicate(self, _require, _existing, _build):
		source = self.source()
		existing = FakeDocument("ENR-EXISTING", status="Planned")
		with patch(MODULE + ".frappe") as frappe_mock, patch(MODULE + "._", side_effect=lambda value: value):
			frappe_mock.get_doc.side_effect = [source, existing]
			frappe_mock.db.get_value.side_effect = [self.target_term(), self.target_timeslot()]
			result = continue_school_admin_enrollment_to_term_data(
				enrollment=source.name,
				payload={"term": "TERM-4", "weekly_timeslot": "WT-TARGET"},
			)

		self.assertFalse(result["created"])
		self.assertEqual(result["enrollment"]["name"], "ENR-EXISTING")
		frappe_mock.new_doc.assert_not_called()
		frappe_mock.db.commit.assert_not_called()

	@patch(MODULE + "._require_school_admin")
	def test_rejects_a_destination_timeslot_for_another_course(self, _require):
		with patch(MODULE + ".frappe") as frappe_mock, patch(MODULE + "._", side_effect=lambda value: value):
			frappe_mock.get_doc.return_value = self.source()
			frappe_mock.db.get_value.side_effect = [self.target_term(), self.target_timeslot("COURSE-2")]
			frappe_mock.throw.side_effect = frappe.ValidationError
			with self.assertRaises(frappe.ValidationError):
				continue_school_admin_enrollment_to_term_data(
					enrollment="ENR-SOURCE",
					payload={"term": "TERM-4", "weekly_timeslot": "WT-TARGET"},
				)
			frappe_mock.db.sql.assert_not_called()
			frappe_mock.new_doc.assert_not_called()

	@patch(MODULE + "._require_school_admin")
	def test_rejects_an_archived_destination_term(self, _require):
		with patch(MODULE + ".frappe") as frappe_mock, patch(MODULE + "._", side_effect=lambda value: value):
			frappe_mock.get_doc.return_value = self.source()
			frappe_mock.db.get_value.return_value = {"name": "TERM-4", "status": "Archived", "start_date": "2026-10-01"}
			frappe_mock.throw.side_effect = frappe.ValidationError
			with self.assertRaises(frappe.ValidationError):
				continue_school_admin_enrollment_to_term_data(
					enrollment="ENR-SOURCE",
					payload={"term": "TERM-4", "weekly_timeslot": "WT-TARGET"},
				)
			frappe_mock.db.sql.assert_not_called()
			frappe_mock.new_doc.assert_not_called()

	@patch(MODULE + "._require_school_admin")
	def test_rejects_a_timeslot_outside_the_destination_term(self, _require):
		wrong_term_timeslot = {**self.target_timeslot(), "term": "TERM-5"}
		with patch(MODULE + ".frappe") as frappe_mock, patch(MODULE + "._", side_effect=lambda value: value):
			frappe_mock.get_doc.return_value = self.source()
			frappe_mock.db.get_value.side_effect = [self.target_term(), wrong_term_timeslot]
			frappe_mock.throw.side_effect = frappe.ValidationError
			with self.assertRaises(frappe.ValidationError):
				continue_school_admin_enrollment_to_term_data(
					enrollment="ENR-SOURCE",
					payload={"term": "TERM-4", "weekly_timeslot": "WT-TARGET"},
				)
			frappe_mock.db.sql.assert_not_called()
			frappe_mock.new_doc.assert_not_called()
