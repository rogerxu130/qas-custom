from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields import (
	_ensure_custom_field,
	execute,
)


class TestNotificationAuditPatch(TestCase):
	@patch("qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields.frappe.clear_cache")
	@patch("qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields._ensure_custom_field")
	@patch("qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields.frappe.db.exists", return_value=True)
	def test_execute_installs_all_delivery_audit_fields(self, _mock_exists, mock_ensure, mock_clear):
		execute()
		fieldnames = [call.args[1]["fieldname"] for call in mock_ensure.call_args_list]
		self.assertEqual(fieldnames, ["recipient_email", "delivery_status", "failure_reason", "sent_at"])
		mock_clear.assert_called_once_with(doctype="Notification Log")

	@patch("qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields.frappe.get_doc")
	@patch(
		"qas_custom.patches.v2026_07_22_add_notification_delivery_audit_fields.frappe.db.exists",
		side_effect=[False, False],
	)
	def test_missing_field_is_created_idempotently(self, _mock_exists, mock_get_doc):
		doc = Mock()
		mock_get_doc.return_value = doc
		values = {"fieldname": "recipient_email", "fieldtype": "Data", "label": "Recipient Email"}
		_ensure_custom_field("Notification Log", values)
		mock_get_doc.assert_called_once_with({"doctype": "Custom Field", "dt": "Notification Log", **values})
		doc.insert.assert_called_once_with(ignore_permissions=True)
