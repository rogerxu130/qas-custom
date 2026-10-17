from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

from qas_custom.services.school_admin import get_school_admin_family_attendance_data

MODULE = 'qas_custom.services.school_admin.'


class TestFamilyAttendanceVisibility(TestCase):
    def test_today_keeps_unmarked_history_and_hides_later_ordinary_classes(self):
        self.check_visibility('2026-10-10', [
            'future-makeup', 'future-cancelled', 'future-leave', 'today-present',
            'today-unmarked', 'past-late', 'past-absent', 'past-present', 'past-unmarked',
        ], ['past-unmarked'])

    def test_no_class_today_shows_only_next_class_day(self):
        self.check_visibility('2026-10-20', [
            'future-unmarked', 'empty-status', 'future-makeup', 'future-cancelled',
            'future-leave', 'today-present', 'today-unmarked', 'past-late',
            'past-absent', 'past-present', 'past-unmarked',
        ], ['future-makeup', 'today-unmarked', 'past-unmarked'])

    def test_cancelled_today_does_not_hide_next_class_day(self):
        self.check_visibility('2026-10-18', [
            'future-makeup', 'future-cancelled', 'future-leave', 'today-present',
            'today-unmarked', 'past-late', 'past-absent', 'past-present', 'past-unmarked',
        ], ['today-unmarked', 'past-unmarked'])

    def test_no_remaining_classes_keeps_all_unmarked_history(self):
        self.check_visibility('2026-11-01', [
            'later-unmarked', 'future-unmarked', 'empty-status', 'future-makeup',
            'future-cancelled', 'future-leave', 'today-present', 'today-unmarked',
            'past-late', 'past-absent', 'past-present', 'past-unmarked',
        ], ['later-unmarked', 'future-unmarked', 'empty-status', 'future-makeup',
            'today-unmarked', 'past-unmarked'])

    def check_visibility(self, current_date, expected_names, needs_attendance):
        cases = [
            ('past-present', '2026-10-03', 'Present', 'Full-Term', 'TERM-1'),
            ('today-present', '2026-10-10', 'Present', 'Full-Term', 'TERM-1'),
            ('future-leave', '2026-10-17', 'Leave', 'Full-Term', 'TERM-1'),
            ('future-cancelled', '2026-10-18', 'Cancelled', 'Full-Term', 'TERM-1'),
            ('past-absent', '2026-10-04', 'Absent', 'Full-Term', 'TERM-1'),
            ('past-late', '2026-10-05', 'Late', 'Full-Term', 'TERM-1'),
            ('future-makeup', '2026-10-19', 'To be started', 'Makeup', 'TERM-1'),
            ('past-unmarked', '2026-10-01', 'To be started', 'Full-Term', 'TERM-1'),
            ('today-unmarked', '2026-10-10', 'To be started', 'Full-Term', 'TERM-1'),
            ('future-unmarked', '2026-10-24', 'To be started', 'Full-Term', 'TERM-1'),
            ('later-unmarked', '2026-10-31', 'To be started', 'Full-Term', 'TERM-1'),
            ('empty-status', '2026-10-24', '', 'Full-Term', 'TERM-1'),
            ('other-term-leave', '2027-01-01', 'Leave', 'Full-Term', 'TERM-2'),
        ]
        attendance = [dict(name=name, student='STUDENT-1', course_session=name, status=status, enrollment_type=kind) for name, date, status, kind, term in cases]
        sessions = [dict(name=name, weekly_timeslot=term, session_date=date) for name, date, status, kind, term in cases]

        def get_all(doctype, **kwargs):
            if doctype == 'Class Attendance Entry':
                self.assertEqual(kwargs['filters']['student'], ['in', ['STUDENT-1']])
                return attendance
            if doctype == 'Course Sessions':
                self.assertNotIn('session_date', kwargs['filters'])
                requested = kwargs['filters']['name'][1]
                return [row for row in sessions if row['name'] in requested]
            if doctype == 'Weekly Timeslot':
                self.assertEqual(kwargs['filters']['term'], 'TERM-1')
                return [dict(name='TERM-1', term='TERM-1', start_time='09:00:00')]
            raise AssertionError(doctype)

        with ExitStack() as stack:
            for name, value in {
                '_resolve_family_context': {'parent': 'PARENT-1'},
                '_get_family_students': [{'name': 'STUDENT-1', 'student_name': 'Student One'}],
                '_get_family_attendance_terms': [{'name': 'TERM-1'}, {'name': 'TERM-2'}],
                '_resolve_family_attendance_term': {'name': 'TERM-1'},
                '_doctype_available': True,
                'today': current_date,
            }.items():
                stack.enter_context(patch(MODULE + name, return_value=value))
            stack.enter_context(patch(MODULE + '_require_school_admin'))
            stack.enter_context(patch(MODULE + '_attach_inquiry_teacher_labels'))
            stack.enter_context(patch(MODULE + 'frappe', SimpleNamespace(get_all=Mock(side_effect=get_all))))
            result = get_school_admin_family_attendance_data(parent='PARENT-1', term='TERM-1')

        self.assertEqual([row['name'] for row in result['items']], expected_names)
        self.assertEqual(result['term']['name'], 'TERM-1')
        self.assertTrue(all(row['student_display'] == 'Student One' for row in result['items']))
        self.assertEqual([row['name'] for row in result['items'] if row['needs_attendance']], needs_attendance)


if __name__ == '__main__':
    main()
