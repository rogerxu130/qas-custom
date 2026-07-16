from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.inquiry import _assert_no_duplicate_trial_inquiry, _get_or_create_user_for_parent
from qas_custom.services.school_admin import (
	_manual_trial_existing_family_payload,
	_manual_trial_new_family_payload,
	create_school_admin_trial_inquiry_data,
)


class TestSchoolAdminManualTrial(TestCase):
	def test_create_uses_shared_core_without_intermediate_commit(self):
		fake_frappe = SimpleNamespace(
			session=SimpleNamespace(user="school.admin@example.com"),
			db=SimpleNamespace(commit=Mock()),
		)
		detail = {"inquiry": {"id": "INQ-2026-00001"}}
		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_payload",
			return_value={"family_mode": "existing", "course_session": "SESSION-001", "note": "Called parent"},
		), patch(
			"qas_custom.services.school_admin._manual_trial_existing_family_payload",
			return_value={"parent": "PARENT-001", "student": "STUDENT-001"},
		), patch(
			"qas_custom.services.school_admin.create_inquiry_core",
			return_value=detail,
		) as create_core, patch(
			"qas_custom.services.school_admin.add_inquiry_note_core",
			return_value=detail,
		) as add_note, patch(
			"qas_custom.services.school_admin.build_inquiry_detail",
			return_value=detail,
		), patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = create_school_admin_trial_inquiry_data()

		payload = create_core.call_args.args[0]
		self.assertEqual(payload["inquiry_type"], "Trial Lesson")
		self.assertTrue(payload["require_bookable_session"])
		self.assertTrue(payload["prevent_duplicate_student_session"])
		self.assertFalse(create_core.call_args.kwargs["commit"])
		add_note.assert_called_once_with(
			"INQ-2026-00001",
			"Called parent",
			actor="school.admin@example.com",
			commit=False,
		)
		fake_frappe.db.commit.assert_called_once()
		self.assertEqual(result, detail)

	def test_wrapper_checks_school_admin_role_first(self):
		with patch(
			"qas_custom.services.school_admin._require_school_admin",
			side_effect=RuntimeError("School Admin only"),
		), patch("qas_custom.services.school_admin.create_inquiry_core") as create_core:
			with self.assertRaisesRegex(RuntimeError, "School Admin only"):
				create_school_admin_trial_inquiry_data({})
		create_core.assert_not_called()

	def test_existing_student_must_belong_to_parent(self):
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(
				exists=Mock(return_value=True),
				get_value=Mock(return_value="PARENT-OTHER"),
			),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.school_admin._student_parent_field", return_value="guardian"), patch(
			"qas_custom.services.school_admin.frappe", fake_frappe
		):
			with self.assertRaisesRegex(RuntimeError, "does not belong"):
				_manual_trial_existing_family_payload({"parent": "PARENT-001", "student": "STUDENT-001"})

	def test_new_family_requires_identity_and_student_fields(self):
		fake_frappe = SimpleNamespace(
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.school_admin.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "Parent email"):
				_manual_trial_new_family_payload({"parent_name": "Parent", "student_name": "Child"})

	@patch("qas_custom.services.school_admin.validate_email_address")
	def test_new_family_normalizes_email_and_keeps_student_inactive(self, validate_email):
		payload = _manual_trial_new_family_payload(
			{
				"parent_name": " Jane Parent ",
				"contact_email": " JANE@EXAMPLE.COM ",
				"contact_phone": "0400 000 000",
				"student_name": " Child Name ",
				"date_of_birth": "2018-02-03",
			}
		)
		validate_email.assert_called_once_with("jane@example.com", throw=True)
		self.assertEqual(payload["parent_name"], "Jane Parent")
		self.assertEqual(payload["contact_email"], "jane@example.com")
		self.assertEqual(payload["student_name"], "Child Name")
		self.assertEqual(payload["student_status"], "Inactive")

	def test_duplicate_active_trial_reports_existing_inquiry(self):
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(get_value=Mock(return_value="INQ-2026-00009")),
			throw=lambda message, *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(str(message))),
		)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			with self.assertRaisesRegex(RuntimeError, "INQ-2026-00009"):
				_assert_no_duplicate_trial_inquiry("STUDENT-001", "SESSION-001")

		filters = fake_frappe.db.get_value.call_args.args[1]
		self.assertEqual(filters["status"], ["not in", ["Cancelled", "Inactive"]])

	def test_no_duplicate_allows_creation(self):
		fake_frappe = SimpleNamespace(db=SimpleNamespace(get_value=Mock(return_value=None)))
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			self.assertIsNone(_assert_no_duplicate_trial_inquiry("STUDENT-001", "SESSION-001"))

	def test_new_parent_user_receives_parent_role_without_welcome_email(self):
		user_doc = SimpleNamespace(
			name="parent@example.com",
			flags=SimpleNamespace(),
			append=Mock(),
			insert=Mock(),
		)
		fake_frappe = SimpleNamespace(
			db=SimpleNamespace(
				exists=Mock(side_effect=lambda doctype, _name: doctype == "Role"),
				get_value=Mock(return_value=None),
			),
			new_doc=Mock(return_value=user_doc),
		)
		with patch("qas_custom.services.inquiry.frappe", fake_frappe):
			result = _get_or_create_user_for_parent("parent@example.com", "Parent Name")

		self.assertEqual(result, "parent@example.com")
		self.assertEqual(user_doc.user_type, "Website User")
		self.assertEqual(user_doc.send_welcome_email, 0)
		user_doc.append.assert_called_once_with("roles", {"role": "Parent"})
		user_doc.insert.assert_called_once()
