from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.services.school_admin import (
	convert_school_admin_school_visit_to_enrollment_data,
	convert_school_admin_school_visit_to_trial_data,
	create_school_admin_school_visit_data,
)


class TestSchoolAdminSchoolVisitCreation(TestCase):
	def test_manual_creation_requires_parent_but_never_creates_student(self):
		detail = {"inquiry": {"id": "INQ-VISIT", "parent": "PAR-001", "student": None, "status": "Booked"}}
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="school.admin@example.com"),
			db=SimpleNamespace(commit=Mock()),
			throw=lambda message, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(str(message))),
		)
		with patch("qas_custom.services.school_admin._require_school_admin") as require_admin, patch(
			"qas_custom.services.school_admin.create_inquiry_core", return_value=detail
		) as create_core, patch(
			"qas_custom.services.school_admin.validate_email_address"
		), patch("qas_custom.services.school_admin._", side_effect=lambda message: message), patch(
			"qas_custom.services.school_admin.frappe", fake_frappe
		):
			result = create_school_admin_school_visit_data(
				{
					"parent_name": "Parent One",
					"contact_email": " Parent@One.Example ",
					"contact_phone": "0400000000",
					"campus": "Indooroopilly",
					"appointment_date": "2026-08-20",
					"appointment_time": "10:00",
				}
			)

		require_admin.assert_called_once_with()
		payload = create_core.call_args.args[0]
		self.assertEqual(payload["inquiry_type"], "School Visit")
		self.assertEqual(payload["contact_email"], "parent@one.example")
		self.assertTrue(payload["create_parent"])
		self.assertNotIn("student", payload)
		self.assertNotIn("student_name", payload)
		self.assertEqual(result["inquiry"]["status"], "Booked")
		fake_frappe.db.commit.assert_called_once_with()


class TestSchoolAdminSchoolVisitConversion(TestCase):
	def _visit(self, **values):
		defaults = {
			"name": "INQ-VISIT",
			"inquiry_type": "School Visit",
			"status": "Completed",
			"parent": "PAR-001",
			"student": None,
			"contact_name": "Parent One",
			"contact_email": "parent@example.com",
			"contact_phone": "0400000000",
			"campus": "Indooroopilly",
			"preferred_course": "Drawing",
			"referral_source": "Website",
			"referral_detail": "",
			"converted_trial_inquiry": "",
			"converted_enrollment": "",
			"converted_invoice": "",
		}
		defaults.update(values)
		doc = frappe._dict(defaults)
		doc.save = Mock()
		return doc

	def test_trial_conversion_creates_separate_inquiry_and_marks_visit_converted(self):
		visit = self._visit()
		trial_doc = frappe._dict(name="INQ-TRIAL", student="STU-001")
		trial_detail = {"inquiry": {"id": "INQ-TRIAL", "student": "STU-001", "status": "Booked"}}
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="school.admin@example.com"),
			db=SimpleNamespace(commit=Mock()),
			get_doc=Mock(return_value=trial_doc),
		)
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_school_visit_for_conversion", return_value=visit
		), patch("qas_custom.services.school_admin._assert_school_visit_conversion_available"), patch(
			"qas_custom.services.school_admin._school_visit_conversion_student_payload", return_value={"student": "STU-001"}
		), patch("qas_custom.services.school_admin.create_inquiry_core", return_value=trial_detail) as create_core, patch(
			"qas_custom.services.school_admin._copy_school_visit_notes"
		), patch("qas_custom.services.school_admin.add_system_note"), patch(
			"qas_custom.services.school_admin.build_inquiry_detail",
			side_effect=lambda name: {"inquiry": {"id": name}},
		), patch("qas_custom.services.school_admin._", side_effect=lambda message: message), patch(
			"qas_custom.services.school_admin.frappe", fake_frappe
		):
			result = convert_school_admin_school_visit_to_trial_data(
				"INQ-VISIT", {"student": "STU-001", "course_session": "SES-TRIAL"}
			)

		trial_payload = create_core.call_args.args[0]
		self.assertEqual(trial_payload["inquiry_type"], "Trial Lesson")
		self.assertEqual(trial_payload["parent"], "PAR-001")
		self.assertEqual(trial_payload["student"], "STU-001")
		self.assertEqual(visit.converted_trial_inquiry, "INQ-TRIAL")
		self.assertEqual(visit.status, "Converted")
		visit.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(result["conversion"]["trial_inquiry"], "INQ-TRIAL")

	def test_trial_conversion_is_idempotent(self):
		visit = self._visit(status="Converted", converted_trial_inquiry="INQ-TRIAL")
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_school_visit_for_conversion", return_value=visit
		), patch(
			"qas_custom.services.school_admin.build_inquiry_detail",
			side_effect=lambda name: {"inquiry": {"id": name}},
		), patch("qas_custom.services.school_admin.create_inquiry_core") as create_core:
			result = convert_school_admin_school_visit_to_trial_data("INQ-VISIT", {})

		self.assertTrue(result["duplicate"])
		self.assertEqual(result["conversion"]["trial_inquiry"], "INQ-TRIAL")
		create_core.assert_not_called()

	def test_direct_enrollment_attaches_student_before_reusing_conversion_core(self):
		visit = self._visit(status="Follow-up")
		conversion = {
			"inquiry": {"inquiry": {"id": "INQ-VISIT", "status": "Converted"}},
			"conversion": {"enrollment": "ENR-001", "invoice": "SINV-001"},
		}
		fake_frappe = SimpleNamespace(session=SimpleNamespace(user="school.admin@example.com"))
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_school_visit_for_conversion", return_value=visit
		), patch("qas_custom.services.school_admin._assert_school_visit_conversion_available"), patch(
			"qas_custom.services.school_admin._resolve_school_visit_conversion_student", return_value="STU-001"
		), patch(
			"qas_custom.services.school_admin.convert_school_visit_to_full_term_core", return_value=conversion
		) as convert_core, patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = convert_school_admin_school_visit_to_enrollment_data(
				"INQ-VISIT", {"student": "STU-001", "course_session": "SES-START", "note": "Keep draft"}
			)

		self.assertEqual(visit.student, "STU-001")
		visit.save.assert_called_once_with(ignore_permissions=True)
		convert_core.assert_called_once_with(
			visit,
			"SES-START",
			actor="school.admin@example.com",
			internal_note="Keep draft",
		)
		self.assertFalse(result["duplicate"])
