from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

from qas_custom.services.school_admin import get_school_admin_family_attendance_data

MODULE = 'qas_custom.services.school_admin.'


class TestFamilyAttendanceVisibility(TestCase):
    def test_term_history_keeps_marked_and_future_special_records(self):
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
                'today': '2026-10-10',
            }.items():
                stack.enter_context(patch(MODULE + name, return_value=value))
            stack.enter_context(patch(MODULE + '_require_school_admin'))
            stack.enter_context(patch(MODULE + '_attach_inquiry_teacher_labels'))
            stack.enter_context(patch(MODULE + 'frappe', SimpleNamespace(get_all=Mock(side_effect=get_all))))
            result = get_school_admin_family_attendance_data(parent='PARENT-1', term='TERM-1')

        self.assertEqual([row['name'] for row in result['items']], [
            'future-makeup', 'future-cancelled', 'future-leave', 'today-present',
            'past-late', 'past-absent', 'past-present',
        ])
        self.assertEqual(result['term']['name'], 'TERM-1')
        self.assertTrue(all(row['student_display'] == 'Student One' for row in result['items']))
        self.assertTrue(all(not row['needs_attendance'] for row in result['items']))


if __name__ == '__main__':
    main()
