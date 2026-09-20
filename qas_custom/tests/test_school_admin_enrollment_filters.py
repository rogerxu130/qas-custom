from unittest import TestCase
from unittest.mock import patch

from qas_custom.services import school_admin as service


class EnrollmentFilterTests(TestCase):
    def setUp(self):
        self.rows = [dict(name=f'old-{i}', student='Haochen', term='T3', status='Active') for i in range(60)]
        self.rows += [dict(name='new', student='Haochen', term='T4', status='Planned'),
                      dict(name='cancelled', student='Haochen', term='T4', status='Cancelled'),
                      dict(name='termless', student='Other', term=None, status='Active')]
        for name, value in [('_require_school_admin', None), ('_doctype_available', True), ('_has_field', True),
                            ('open_enrollment_or_filters', [['term', 'in', ['T4']], ['term', 'is', 'not set']])]:
            patcher = patch.object(service, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('_normalize_row_payload', '_attach_course_labels'):
            patcher = patch.object(service, name, side_effect=lambda *args: args[-1])
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(service, '_safe_fields', side_effect=lambda dt, fields: fields)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(service.frappe, 'get_all', side_effect=self.get_all)
        self.query = patcher.start()
        self.addCleanup(patcher.stop)

    def match(self, row, condition):
        field, operator, value = condition[-3:]
        actual = row.get(field)
        if operator == 'in': return actual in value
        if operator == 'is': return not actual
        if operator == 'like': return value.strip('%').lower() in str(actual or '').lower()
        return actual == value

    def get_all(self, doctype, filters=None, or_filters=None, pluck=None, limit=0, **kwargs):
        conditions = [[key, *(value if isinstance(value, list) else ['=', value])] for key, value in (filters or {}).items()]
        rows = [row for row in self.rows if all(self.match(row, c) for c in conditions)
                and (not or_filters or any(self.match(row, c) for c in or_filters))]
        if limit: rows = rows[:limit]
        return [row[pluck] for row in rows] if pluck else rows

    def names(self, **kwargs):
        return [row['name'] for row in service.get_school_admin_enrollments_data(**kwargs)['items']]

    def test_search_applies_open_scope_before_limit_even_with_many_historical_matches(self):
        self.assertEqual(self.names(query='haochen', limit=1), ['new'])
        self.assertEqual(self.query.call_args_list[0].kwargs['limit_page_length'], 0)

    def test_all_terms_still_honors_enrollment_status(self):
        self.assertEqual(len(self.names(query='haochen', include_inactive_terms=1)), 61)
        self.assertNotIn('cancelled', self.names(query='haochen', include_inactive_terms=1))

    def test_explicit_closed_term_and_status_are_combined_with_search(self):
        self.assertEqual(len(self.names(query='haochen', term='T3', status='Active')), 60)
        self.assertEqual(self.names(query='haochen', term='T3', status='Planned'), [])
        self.assertEqual(self.names(query='missing', term='T3'), [])

    def test_all_statuses_does_not_implicitly_include_closed_terms(self):
        self.assertEqual(self.names(query='haochen', statuses='Planned,Active,Cancelled,Completed,Inactive'), ['new', 'cancelled'])

    def test_no_search_and_termless_records_preserve_existing_scope(self):
        self.assertEqual(self.names(), ['new', 'termless'])
        self.assertEqual(self.names(query='other'), ['termless'])
