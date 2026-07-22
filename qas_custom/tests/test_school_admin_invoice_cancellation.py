from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.school_admin import (
	bulk_school_admin_invoice_action_data,
	cancel_school_admin_invoice_data,
	delete_school_admin_draft_invoice_data,
)


def _raise_value_error(message):
	raise ValueError(message)


class TestSchoolAdminInvoiceCancellation(TestCase):
	def test_draft_invoice_cannot_be_cancelled(self):
		doc = SimpleNamespace(name="ACC-SINV-2026-00402", docstatus=0)
		fake_frappe = SimpleNamespace(
			get_doc=Mock(return_value=doc),
			throw=Mock(side_effect=_raise_value_error),
			db=SimpleNamespace(commit=Mock(), set_value=Mock()),
		)

		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._", side_effect=lambda message, *args, **kwargs: message,
		), patch(
			"qas_custom.services.school_admin._clear_deleted_invoice_enrollment_snapshot",
		), patch(
			"qas_custom.services.school_admin._build_invoice_payload", return_value={},
		), patch(
			"qas_custom.services.school_admin.frappe", fake_frappe,
		):
			with self.assertRaisesRegex(ValueError, "Draft invoices cannot be cancelled"):
				cancel_school_admin_invoice_data(invoice=doc.name, reason="Created by mistake")

		fake_frappe.db.commit.assert_not_called()
		fake_frappe.db.set_value.assert_not_called()

	def test_mixed_bulk_cancel_is_rejected_before_any_invoice_is_cancelled(self):
		docs = {
			"ACC-SINV-2026-00401": SimpleNamespace(name="ACC-SINV-2026-00401", docstatus=1),
			"ACC-SINV-2026-00402": SimpleNamespace(name="ACC-SINV-2026-00402", docstatus=0),
		}
		fake_frappe = SimpleNamespace(
			get_doc=Mock(side_effect=lambda _doctype, name: docs[name]),
			throw=Mock(side_effect=_raise_value_error),
			db=SimpleNamespace(rollback=Mock()),
		)

		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_payload", side_effect=lambda payload: payload,
		), patch(
			"qas_custom.services.school_admin._", side_effect=lambda message, *args, **kwargs: message,
		), patch(
			"qas_custom.services.school_admin.cancel_school_admin_invoice_data",
			return_value={"status": "Cancelled", "docstatus": 2},
		) as cancel_invoice, patch("qas_custom.services.school_admin.frappe", fake_frappe):
			with self.assertRaisesRegex(ValueError, "submitted invoices only"):
				bulk_school_admin_invoice_action_data(
					payload={
						"action": "cancel",
						"invoices": list(docs),
						"reason": "Duplicate invoice",
					}
				)

		cancel_invoice.assert_not_called()

	def test_submitted_only_bulk_cancel_continues_after_preflight(self):
		doc = SimpleNamespace(name="ACC-SINV-2026-00401", docstatus=1)
		fake_frappe = SimpleNamespace(
			get_doc=Mock(return_value=doc),
			throw=Mock(side_effect=_raise_value_error),
			db=SimpleNamespace(rollback=Mock()),
		)

		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._get_payload", side_effect=lambda payload: payload,
		), patch(
			"qas_custom.services.school_admin._", side_effect=lambda message, *args, **kwargs: message,
		), patch(
			"qas_custom.services.school_admin.cancel_school_admin_invoice_data",
			return_value={"status": "Cancelled", "docstatus": 2},
		) as cancel_invoice, patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = bulk_school_admin_invoice_action_data(
				payload={"action": "cancel", "invoices": [doc.name], "reason": "Duplicate invoice"}
			)

		cancel_invoice.assert_called_once_with(invoice=doc.name, reason="Duplicate invoice")
		self.assertEqual(result["succeeded"], 1)
		self.assertEqual(result["failed"], 0)

	def test_legacy_cancelled_draft_detaches_operation_report_link_before_delete(self):
		doc = SimpleNamespace(name="ACC-SINV-2026-00402", docstatus=0, status="Cancelled")
		report_row = {
			"name": "QORR-00001",
			"parent": "QOR-2026-00039",
			"message": "Invoice reset completed.",
			"reference_doctype": "Enrollment",
			"reference_name": "ENR-2026-00001",
			"raw_row_json": "{}",
		}
		fake_db = SimpleNamespace(set_value=Mock(), commit=Mock())
		fake_frappe = SimpleNamespace(
			get_doc=Mock(return_value=doc),
			get_all=Mock(return_value=[report_row]),
			delete_doc=Mock(),
			db=fake_db,
		)

		with patch("qas_custom.services.school_admin._require_school_admin"), patch(
			"qas_custom.services.school_admin._doctype_available", return_value=True,
		), patch(
			"qas_custom.services.school_admin._", side_effect=lambda message, *args, **kwargs: message,
		), patch(
			"qas_custom.services.school_admin._clear_deleted_invoice_enrollment_snapshot",
		) as clear_enrollment, patch("qas_custom.services.school_admin.frappe", fake_frappe):
			result = delete_school_admin_draft_invoice_data(invoice=doc.name)

		fake_db.set_value.assert_called_once()
		set_value_args = fake_db.set_value.call_args.args
		self.assertEqual(set_value_args[:2], ("QAS Operation Report Row", report_row["name"]))
		self.assertIsNone(set_value_args[2]["invoice"])
		self.assertIn(doc.name, set_value_args[2]["message"])
		clear_enrollment.assert_called_once_with(doc)
		fake_frappe.delete_doc.assert_called_once_with("Sales Invoice", doc.name, ignore_permissions=True)
		fake_db.commit.assert_called_once()
		self.assertEqual(result, {"deleted": doc.name})
