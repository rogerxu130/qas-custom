from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.modules.billing.invoice_settings import get_invoice_settings, update_invoice_settings, validate_reminder_interval
from qas_custom.qas_custom.doctype.qas_invoice_settings.qas_invoice_settings import QASInvoiceSettings

MODULE = 'qas_custom.modules.billing.invoice_settings'


class TestInvoiceReminderSettings(TestCase):
    def test_default_before_migration(self):
        with patch(MODULE+'.settings_doctype_available', return_value=False):
            self.assertEqual(get_invoice_settings()['overdue_reminder_interval_days'], 4)

    def test_read_and_save_editable_interval(self):
        doc=Mock()
        values={'overdue_reminder_interval_days': 4}
        doc.get.side_effect=values.get
        doc.set.side_effect=lambda key,value: values.update({key:value})
        with patch(MODULE+'.settings_doctype_available', return_value=True), patch(MODULE+'.frappe.get_single', return_value=doc):
            self.assertEqual(get_invoice_settings()['overdue_reminder_interval_days'], 4)
            result=update_invoice_settings({'overdue_reminder_interval_days': 6})
            self.assertEqual(result['overdue_reminder_interval_days'], 6)
            doc.save.assert_called_once_with(ignore_permissions=True)
            doc.set.assert_called_once_with('overdue_reminder_interval_days', 6)

    def test_api_rejects_invalid_values_before_saving(self):
        for value in (0, -1, 1.5, '', None, 'abc'):
            with self.subTest(value=value), patch(MODULE+'.settings_doctype_available', return_value=True), patch(
                MODULE+'.frappe.get_single', return_value=Mock()) as get_doc, patch(
                MODULE+'.frappe.throw', side_effect=frappe.ValidationError):
                with self.assertRaises(frappe.ValidationError):
                    update_invoice_settings({'overdue_reminder_interval_days': value})
                get_doc.return_value.save.assert_not_called()

    def test_native_frappe_form_uses_same_validation(self):
        doc=frappe._dict(overdue_reminder_interval_days=0)
        with patch(MODULE+'.frappe.throw', side_effect=frappe.ValidationError):
            with self.assertRaises(frappe.ValidationError):
                QASInvoiceSettings.validate(doc)
        doc.overdue_reminder_interval_days=7
        QASInvoiceSettings.validate(doc)
        self.assertEqual(doc.overdue_reminder_interval_days, 7)
