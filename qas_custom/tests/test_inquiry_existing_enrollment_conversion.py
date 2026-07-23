from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from qas_custom.modules.workflows.trial_conversion import link_existing_enrollment_core
from qas_custom.services.school_admin import link_school_admin_inquiry_enrollment_data


class TestInquiryExistingEnrollmentConversion(TestCase):
	def _doc(self, **values):
		doc = frappe._dict(values)
		doc.meta = SimpleNamespace(has_field=Mock(return_value=True))
		doc.save = Mock()
		return doc

	def _run(self, inquiry=None, enrollment=None, converted_conflict=None):
		inquiry = inquiry or self._doc(
			name="INQ-001",
			inquiry_type="Trial Lesson",
			status="Completed",
			student="STU-001",
			parent="PAR-001",
			converted_enrollment="",
			converted_invoice="",
		)
		enrollment = enrollment or self._doc(
			name="ENR-001",
			status="Active",
			student="STU-001",
			parent="PAR-001",
			source_inquiry="",
			invoice="SINV-001",
		)
		db = SimpleNamespace(get_value=Mock(return_value=converted_conflict), commit=Mock())
		fake_frappe = SimpleNamespace(
			db=db,
			get_doc=Mock(side_effect=lambda doctype, _name: inquiry if doctype == "Inquiry" else enrollment),
			throw=lambda message, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(str(message))),
			session=SimpleNamespace(user="school.admin@example.com"),
		)
		with patch("qas_custom.modules.workflows.trial_conversion.frappe", fake_frappe), patch(
			"qas_custom.modules.workflows.trial_conversion.add_system_note"
		) as add_note, patch(
			"qas_custom.services.inquiry.build_inquiry_detail",
			return_value={"inquiry": {"id": inquiry.name, "status": "Converted"}},
		) as build_detail:
			result = link_existing_enrollment_core(inquiry.name, enrollment.name, actor="school.admin@example.com")
		return result, inquiry, enrollment, db, add_note, build_detail

	def test_links_existing_enrollment_without_creating_conversion_records(self):
		result, inquiry, enrollment, db, add_note, build_detail = self._run()

		self.assertEqual(inquiry.status, "Converted")
		self.assertEqual(inquiry.converted_enrollment, "ENR-001")
		self.assertEqual(inquiry.converted_invoice, "SINV-001")
		self.assertEqual(enrollment.source_inquiry, "INQ-001")
		enrollment.save.assert_called_once_with(ignore_permissions=True)
		inquiry.save.assert_called_once_with(ignore_permissions=True)
		add_note.assert_called_once()
		self.assertIn("no new Enrollment, invoice, or attendance", add_note.call_args.kwargs["note"])
		db.commit.assert_called_once()
		build_detail.assert_called_once_with("INQ-001")
		self.assertEqual(result["inquiry"]["status"], "Converted")

	def test_existing_source_inquiry_is_kept_when_it_matches(self):
		enrollment = self._doc(
			name="ENR-001",
			status="Planned",
			student="STU-001",
			parent="PAR-001",
			source_inquiry="INQ-001",
			invoice="",
		)

		_result, inquiry, enrollment, _db, _note, _detail = self._run(enrollment=enrollment)

		enrollment.save.assert_not_called()
		self.assertEqual(inquiry.converted_invoice, "")

	def test_repeat_link_to_same_enrollment_is_idempotent(self):
		inquiry = self._doc(
			name="INQ-001",
			inquiry_type="Trial Lesson",
			status="Converted",
			student="STU-001",
			parent="PAR-001",
			converted_enrollment="ENR-001",
			converted_invoice="SINV-001",
		)

		_result, inquiry, enrollment, db, add_note, build_detail = self._run(inquiry=inquiry)

		inquiry.save.assert_not_called()
		enrollment.save.assert_not_called()
		add_note.assert_not_called()
		db.commit.assert_not_called()
		build_detail.assert_called_once_with("INQ-001")

	def test_rejects_enrollment_for_another_student(self):
		enrollment = self._doc(
			name="ENR-001",
			status="Active",
			student="STU-OTHER",
			parent="PAR-001",
			source_inquiry="",
			invoice="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "same Student"):
			self._run(enrollment=enrollment)

	def test_rejects_parent_mismatch_when_both_are_present(self):
		enrollment = self._doc(
			name="ENR-001",
			status="Active",
			student="STU-001",
			parent="PAR-OTHER",
			source_inquiry="",
			invoice="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "same Parent"):
			self._run(enrollment=enrollment)

	def test_rejects_cancelled_enrollment(self):
		enrollment = self._doc(
			name="ENR-001",
			status="Cancelled",
			student="STU-001",
			parent="PAR-001",
			source_inquiry="",
			invoice="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Planned or Active"):
			self._run(enrollment=enrollment)

	def test_rejects_enrollment_linked_to_another_inquiry(self):
		enrollment = self._doc(
			name="ENR-001",
			status="Active",
			student="STU-001",
			parent="PAR-001",
			source_inquiry="INQ-OTHER",
			invoice="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "INQ-OTHER"):
			self._run(enrollment=enrollment)

	def test_rejects_enrollment_used_by_another_converted_inquiry(self):
		with self.assertRaisesRegex(frappe.ValidationError, "INQ-OTHER"):
			self._run(converted_conflict="INQ-OTHER")

	def test_rejects_non_trial_inquiry(self):
		inquiry = self._doc(
			name="INQ-001",
			inquiry_type="School Visit",
			status="Completed",
			student="STU-001",
			parent="PAR-001",
			converted_enrollment="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Trial Lesson"):
			self._run(inquiry=inquiry)

	def test_rejects_inquiry_outside_post_visit_statuses(self):
		inquiry = self._doc(
			name="INQ-001",
			inquiry_type="Trial Lesson",
			status="Booked",
			student="STU-001",
			parent="PAR-001",
			converted_enrollment="",
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Completed or Follow-up"):
			self._run(inquiry=inquiry)

	def test_school_admin_wrapper_checks_role_before_linking(self):
		with patch("qas_custom.services.school_admin._require_school_admin") as require_school_admin, patch(
			"qas_custom.services.school_admin.link_existing_enrollment_core",
			return_value={"inquiry": {"status": "Converted"}},
		) as link_core, patch(
			"qas_custom.services.school_admin.frappe",
			SimpleNamespace(session=SimpleNamespace(user="school.admin@example.com")),
		):
			result = link_school_admin_inquiry_enrollment_data("INQ-001", "ENR-001")

		require_school_admin.assert_called_once_with()
		link_core.assert_called_once_with("INQ-001", "ENR-001", actor="school.admin@example.com")
		self.assertEqual(result["inquiry"]["status"], "Converted")
