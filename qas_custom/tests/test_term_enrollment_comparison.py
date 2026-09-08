from unittest import TestCase
from unittest.mock import patch
from qas_custom.services import term_enrollment_comparison as report


def enr(name='E1', term='T3', student='S1', course='Art', slot='W1', **extra):
    return dict(name=name, term=term, student=student, course=course, weekly_timeslot=slot,
                enrollment_type='Full-Term', status='Active', campus='City', day_of_week='Saturday',
                start_time='10:00:00', end_time='11:00:00', class_language='English', **extra)


class TestTermEnrollmentComparison(TestCase):
    def test_students_only_on_one_side_and_cancelled_context(self):
        rows = [enr(), {**enr('cancel', 'T4'), 'status': 'Cancelled'}, enr('new', 'T4', 'S2')]
        result = report.build_comparison(rows, 'T3', 'T4', ['Active'])
        self.assertEqual(result[0]['tags'], ['not_continuing'])
        self.assertEqual(result[0]['excluded_after'][0]['name'], 'cancel')
        self.assertEqual(result[1]['tags'], ['new_to_term'])

    def test_copied_class_schedule_change_and_increase(self):
        before = enr()
        after = {**enr('E2', 'T4', slot='W2', copied_from_weekly_timeslot='W1'), 'start_time': '12:00:00'}
        result = report.compare_student([before], [after, enr('E3', 'T4', course='Clay', slot='W3')])
        self.assertEqual(result['tags'], ['increased', 'changed'])
        self.assertEqual(result['changed'][0]['changed_fields'], ['start_time'])
        self.assertEqual(result['added'][0]['course'], 'Clay')

    def test_reverse_comparison_and_reduction(self):
        source = enr()
        target = enr('E2', 'T4', slot='W2', copied_from_weekly_timeslot='W1')
        result = report.compare_student([target, enr('E3', 'T4', course='Clay', slot='W3')], [source])
        self.assertEqual(result['tags'], ['decreased'])
        self.assertEqual(result['retained'][0]['basis'], 'copied_class')
        self.assertEqual(result['removed'][0]['course'], 'Clay')

    def test_equal_class_details_with_different_ids_are_unchanged(self):
        result = report.compare_student([enr()], [enr('E2', 'T4', slot='W9')])
        self.assertEqual(result['tags'], ['unchanged'])

    def test_unique_same_course_transfer_and_increase(self):
        moved = {**enr('E2', 'T4', slot='W2'), 'campus': 'South'}
        result = report.compare_student([enr()], [moved, enr('E3', 'T4', course='Clay', slot='W3')])
        self.assertEqual(result['tags'], ['increased', 'changed'])
        self.assertEqual(result['changed'][0]['basis'], 'same_course')

    def test_single_course_replacement(self):
        result = report.compare_student([enr()], [enr('E2', 'T4', course='Clay', slot='W2')])
        self.assertEqual(result['tags'], ['changed'])
        self.assertEqual(result['changed'][0]['basis'], 'single_remaining_pair')

    def test_ambiguous_multiple_changes_not_paired(self):
        before = [enr(), enr('E2', course='Clay', slot='W2')]
        after = [enr('E3', 'T4', course='Drawing', slot='W3'), enr('E4', 'T4', course='Painting', slot='W4')]
        result = report.compare_student(before, after)
        self.assertEqual(result['tags'], ['needs_review'])
        self.assertEqual(len(result['added']), 2)
        self.assertEqual(len(result['removed']), 2)
        self.assertFalse(result['changed'])

    def test_duplicates_and_missing_details_need_review(self):
        result = report.compare_student([enr(), enr('E2')], [enr('E3', 'T4', slot='W3')])
        self.assertIn('needs_review', result['tags'])
        result = report.compare_student([enr(slot='')], [enr('E3', 'T4', slot='')])
        self.assertIn('needs_review', result['tags'])
        self.assertFalse(result['retained'])

    def test_status_scope_and_types(self):
        rows = [enr(), {**enr('E2', 'T4'), 'status': 'Planned'}, {**enr('E3', 'T4', 'S2'), 'enrollment_type': 'Trial'}]
        without = report.build_comparison(rows, 'T3', 'T4', ['Active'])
        self.assertEqual(len(without), 1)
        self.assertEqual(without[0]['tags'], ['not_continuing'])
        with_planned = report.build_comparison(rows, 'T3', 'T4', ['Active', 'Planned'])
        self.assertEqual(with_planned[0]['tags'], ['unchanged'])
        self.assertEqual(len(report.build_comparison(rows, 'T3', 'T4', ['Active'], '')), 2)

    def test_complete_population_over_200_and_student_dedup(self):
        rows = [enr(f'E{i}', student=f'S{i}') for i in range(501)]
        rows.append(enr('extra', student='S0', course='Clay', slot='W2'))
        result = report.build_comparison(rows, 'T3', 'T4', ['Active'])
        self.assertEqual(len(result), 501)
        self.assertEqual(result[0]['before_count'], 2)

    @patch.object(report, '_require_school_admin', side_effect=PermissionError)
    @patch.object(report.frappe, 'get_all')
    def test_permission_checked_before_data_access(self, get_all, guard):
        with self.assertRaises(PermissionError):
            report.get_term_enrollment_comparison('T3', 'T4')
        get_all.assert_not_called()

    @patch.object(report, '_require_school_admin')
    @patch.object(report, '_validate_term')
    @patch.object(report.frappe, '_', side_effect=lambda text: text)
    def test_endpoint_full_query_and_summary(self, translate, validate, guard):
        rows = [enr(f'E{i}', student=f'S{i}') for i in range(501)]
        def get_all(doctype, **kwargs):
            self.assertEqual(kwargs['limit_page_length'], 0)
            if doctype == 'Enrollment':
                self.assertEqual(kwargs['filters'], {'term': ['in', ['T3', 'T4']]})
                return rows
            return []
        with patch.object(report.frappe, 'get_all', side_effect=get_all), patch.object(report, '_safe_fields', side_effect=lambda dt, fields: fields), patch.object(report, '_student_map', return_value={}), patch.object(report, '_parent_map', return_value={}), patch.object(report, 'now_datetime', return_value='2026-09-08'):
            result = report.get_term_enrollment_comparison('T3', 'T4')
        self.assertEqual(len(result['items']), 501)
        self.assertEqual(result['summary']['not_continuing'], 501)
        self.assertEqual(result['statuses'], ['Active', 'Completed', 'Planned'])

    @patch.object(report, '_require_school_admin')
    @patch.object(report, '_validate_term')
    @patch.object(report, '_', side_effect=lambda text: text)
    @patch.object(report.frappe, 'throw', side_effect=ValueError)
    @patch.object(report.frappe, 'get_all')
    def test_equal_terms_and_invalid_scope_fail_before_read(self, get_all, throw, translate, validate, guard):
        for args in [dict(source_term='T3', target_term='T3'), dict(source_term='T3', target_term='T4', enrollment_type='unexpected')]:
            with self.assertRaises(ValueError):
                report.get_term_enrollment_comparison(**args)
        get_all.assert_not_called()
