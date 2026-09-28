from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

from qas_custom.services.school_admin import get_school_admin_family_attendance_data

MODULE = 'qas_custom.services.school_admin.'


class TestFamilyAttendanceTeacher(TestCase):
    def test_teacher_assignment_on_family_attendance(self):
        for override, weekly, expected, source in [
            ('T-SUB', 'T-REGULAR', 'Substitute Teacher', 'Session override'),
            ('', 'T-REGULAR', 'Regular Teacher', 'Weekly timeslot'),
            ('', '', '', ''),
        ]:
            with self.subTest(override=override, weekly=weekly):
                def get_all(doctype, **kwargs):
                    if doctype == 'Class Attendance Entry':
                        self.assertEqual(kwargs['filters']['student'], ['in', ['STUDENT-1']])
                        return [{'name': 'ATT-1', 'student': 'STUDENT-1', 'course_session': 'CS-1', 'status': 'Present'}]
                    if doctype == 'Course Sessions':
                        if 'session_date' in kwargs['filters']:
                            self.assertEqual(kwargs['filters']['session_date'], ['<=', '2026-09-28'])
                        return [{'name': 'CS-1', 'weekly_timeslot': 'WT-1', 'session_date': '2026-09-20', 'teacher_override': override}]
                    if doctype == 'Weekly Timeslot':
                        if 'term' in kwargs['filters']:
                            self.assertEqual(kwargs['filters']['term'], 'TERM-1')
                        return [{'name': 'WT-1', 'term': 'TERM-1', 'teacher': weekly}]
                    if doctype == 'Teacher':
                        return [{'name': 'T-SUB', 'teacher_name': 'Substitute Teacher'}, {'name': 'T-REGULAR', 'teacher_name': 'Regular Teacher'}]
                    raise AssertionError(doctype)

                with patch(MODULE + '_require_school_admin'), \
                     patch(MODULE + '_resolve_family_context', return_value={'parent': 'PARENT-1'}), \
                     patch(MODULE + '_get_family_students', return_value=[{'name': 'STUDENT-1', 'student_name': 'Student One'}]), \
                     patch(MODULE + '_get_family_attendance_terms', return_value=[{'name': 'TERM-1'}]), \
                     patch(MODULE + '_resolve_family_attendance_term', return_value={'name': 'TERM-1'}), \
                     patch(MODULE + '_doctype_available', return_value=True), \
                     patch(MODULE + 'today', return_value='2026-09-28'), \
                     patch(MODULE + 'frappe', SimpleNamespace(get_all=Mock(side_effect=get_all))):
                    result = get_school_admin_family_attendance_data(parent='PARENT-1', term='TERM-1')
                self.assertEqual(len(result['items']), 1)
                row = result['items'][0]
                self.assertEqual(row['teacher'], override or weekly)
                self.assertEqual(row['teacher_display'], expected)
                self.assertEqual(row['teacher_assignment_source'], source)
                self.assertEqual(row['student_display'], 'Student One')
                self.assertEqual(row['status'], 'Present')


if __name__ == '__main__':
    main()
