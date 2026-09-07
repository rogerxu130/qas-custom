import json
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from frappe.model.naming import set_name_from_naming_options

ROOT = Path(__file__).resolve().parents[1] / 'qas_custom' / 'doctype'


class TestClassRecordNaming(TestCase):
    def setUp(self):
        parser = patch("frappe.model.naming.has_custom_parser", return_value=None)
        parser.start()
        self.addCleanup(parser.stop)

    def definition(self, kind):
        return json.loads((ROOT / kind / (kind + '.json')).read_text())

    def counter(self):
        counts = defaultdict(int)
        def next_number(prefix, digits):
            counts[prefix] += 1
            return str(counts[prefix]).zfill(digits)
        return next_number

    def test_long_course_and_term_create_distinct_short_timeslot_names(self):
        definition = self.definition('weekly_timeslot')
        values = dict(doctype='Weekly Timeslot', term='2026 Term 3 Makeup', course='Makeup - ' + 'Realistic Art beginner, Designer, ' * 3, campus='Upper Mount Gravatt', day_of_week='Saturday', start_time='09:00:00', teacher='')
        with patch('frappe.model.naming.getseries', side_effect=self.counter()), patch('frappe.model.naming.now_datetime', return_value=datetime(2026, 9, 7)):
            first, second = frappe._dict(values), frappe._dict(values)
            set_name_from_naming_options(definition['autoname'], first)
            set_name_from_naming_options(definition['autoname'], second)
        self.assertLessEqual(len(first.name), 140)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.course, values['course'])
        self.assertEqual(first.term, values['term'])

    def test_session_for_max_length_legacy_timeslot_fits_and_keeps_link(self):
        definition = self.definition('course_sessions')
        doc = frappe._dict(doctype='Course Sessions', weekly_timeslot='L' * 140, session_date='2026-09-19')
        with patch('frappe.model.naming.getseries', side_effect=self.counter()), patch('frappe.model.naming.now_datetime', return_value=datetime(2026, 9, 7)):
            set_name_from_naming_options(definition['autoname'], doc)
        self.assertLessEqual(len(doc.name), 140)
        self.assertEqual(doc.weekly_timeslot, 'L' * 140)
        self.assertEqual(doc.session_date, '2026-09-19')

    def test_generating_existing_session_uses_links_not_old_name_format(self):
        from qas_custom.services.school_admin import _ensure_course_session
        with patch('qas_custom.services.school_admin.frappe.db', new=Mock()) as db, patch('qas_custom.services.school_admin.frappe.new_doc') as new_doc:
            db.exists.return_value = 'legacy-session-name'
            result = _ensure_course_session('WTS-2026-00001', '2026-09-19')
        self.assertEqual(result, {'name': 'legacy-session-name', 'created': False})
        self.assertEqual(db.exists.call_args.args[1]['weekly_timeslot'], 'WTS-2026-00001')
        new_doc.assert_not_called()
