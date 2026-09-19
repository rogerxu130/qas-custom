from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.modules.billing import invoice_settings as settings
from qas_custom.patches.v2026_09_19_configurable_course_due_days import execute

MODULE = settings.__name__


class TestConfigurableDueDays(TestCase):
    def test_custom_boundary_grace_and_zero_lead(self):
        with patch(MODULE + '.get_invoice_settings', return_value={'course_due_lead_days': 10, 'course_due_grace_days': 5}):
            for posting, mid, expected in [('2026-10-01', False, '2026-10-02'), ('2026-10-02', False, '2026-10-02'), ('2026-10-03', False, '2026-10-08'), ('2026-10-01', True, '2026-10-06')]:
                self.assertEqual(str(settings.course_invoice_due_date(posting, '2026-10-12', mid_term=mid)), expected)
        with patch(MODULE + '.get_invoice_settings', return_value={'course_due_lead_days': 0, 'course_due_grace_days': 2}):
            self.assertEqual(str(settings.course_invoice_due_date('2026-10-12', '2026-10-12')), '2026-10-12')
            self.assertEqual(str(settings.course_invoice_due_date('2026-10-13', '2026-10-12')), '2026-10-15')

    def test_validation(self):
        for field, minimum in [('course_due_lead_days', 0), ('course_due_grace_days', 1)]:
            self.assertEqual(settings.validate_course_due_days(str(minimum), field), minimum)
            self.assertEqual(settings.validate_course_due_days(None, field), settings.DEFAULT_INVOICE_SETTINGS[field])
            for value in [minimum - 1, 1.5, 'abc', 'nan', 'inf']:
                with self.subTest(field=field, value=value), patch(MODULE + '.frappe.throw', side_effect=ValueError):
                    with self.assertRaises(ValueError):
                        settings.validate_course_due_days(value, field)

    def test_api_round_trip_preserves_zero_and_fallback(self):
        doc = frappe._dict(payment_due_days=5, course_due_lead_days=0, course_due_grace_days=6)
        doc.set = lambda field, value: doc.update({field: value})
        doc.save = Mock()
        with patch(MODULE + '.settings_doctype_available', return_value=True), patch(MODULE + '.frappe.get_single', return_value=doc):
            result = settings.update_invoice_settings({'course_due_lead_days': 0, 'course_due_grace_days': 4})
            self.assertEqual(result['course_due_lead_days'], 0)
            self.assertEqual(result['course_due_grace_days'], 4)
            self.assertEqual(result['payment_due_days'], 5)
            self.assertEqual(str(settings.get_default_invoice_due_date('2026-10-01')), '2026-10-06')

    def test_migration_initializes_only_missing_fields(self):
        for existing, expected in [({}, {'course_due_lead_days': 7, 'course_due_grace_days': 3}),
                                   ({'course_due_lead_days': '0', 'course_due_grace_days': '6'}, {'course_due_lead_days': '0', 'course_due_grace_days': '6'})]:
            values = dict(existing, payment_due_days='5')
            db = Mock()
            db.get_value.side_effect = lambda doctype, filters, field: values.get(filters['field'])
            db.set_single_value.side_effect = lambda doctype, field, value: values.update({field: value})
            with patch('frappe.db', db):
                execute()
                writes = db.set_single_value.call_count
                execute()
                self.assertEqual(db.set_single_value.call_count, writes)
            self.assertEqual(values, dict(expected, payment_due_days='5'))

    def test_existing_submitted_trial_keeps_agreed_deadline(self):
        from qas_custom.services import trial_invoice
        inquiry = frappe._dict(name='INQ', trial_invoice='INV')
        invoice = frappe._dict(name='INV', docstatus=1, due_date='2026-10-01')
        module = trial_invoice.__name__
        with patch(module + '.frappe.get_doc', side_effect=[inquiry, invoice]), patch(module + '._is_eligible', return_value=True), patch(module + '._link_invoice'), patch(module + '.sync_trial_invoice_dates') as sync, patch(module + '._check_rescheduled_trial_fee'), patch(module + '.resolve_data_issue'), patch(module + '.frappe.db', Mock()), patch(module + '._', side_effect=lambda x: x), patch(module + '._status_payload'):
            trial_invoice._create_trial_invoice('INQ')
        sync.assert_not_called()
        self.assertEqual(invoice.due_date, '2026-10-01')
